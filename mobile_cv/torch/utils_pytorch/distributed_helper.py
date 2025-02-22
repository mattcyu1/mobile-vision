#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

"""
Helper for distributed training in pytorch
adapted from d2/d2go
"""

import contextlib
import functools
import logging
import os
import tempfile
import time
import types
from datetime import timedelta
from typing import Any, Callable, Dict, Optional, Tuple

import mobile_cv.torch.utils_pytorch.comm as comm
import torch
import torch.distributed as dist
import torch.distributed.launcher as pet
import torch.multiprocessing as mp
from mobile_cv.common.misc.py import PicklableWrapper

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = timedelta(minutes=30)
DEFAULT_UNITTEST_TIMEOUT = timedelta(minutes=1)


def get_mp_context():
    """spawn is used for launching processes in `launch`"""
    return mp.get_context("spawn")


class DistributedParams(object):
    """store information about ranks and sizes"""

    def __init__(
        self,
        local_rank: int,
        global_rank: int,
        machine_rank: int,
        num_processes_per_machine: int,
        world_size: int,
    ):
        self.local_rank: int = local_rank
        self.global_rank: int = global_rank
        self.machine_rank: int = machine_rank
        self.num_processes_per_machine: int = num_processes_per_machine
        self.world_size: int = world_size
        self.validate()

    def validate(self):
        # assume same number of processes per machine
        if (
            self.global_rank
            != self.machine_rank * self.num_processes_per_machine + self.local_rank
        ):
            raise ValueError(f"{self} is not valid!")

    @staticmethod
    def from_environ() -> "DistributedParams":
        # Read environment variables according to the contract in:
        # https://pytorch.org/elastic/0.2.0rc1/distributed.html
        # Note that this is a superset of required environment variables of:
        # https://pytorch.org/docs/stable/distributed.html#environment-variable-initialization

        def _get_key(key, default):
            if key not in os.environ:
                logger.warning(
                    f"Can't find {key} in os.environ, use default: {default}"
                )
            return os.environ.get(key, default)

        local_rank = int(_get_key("LOCAL_RANK", 0))
        global_rank = int(_get_key("RANK", 0))
        machine_rank = int(_get_key("GROUP_RANK", 0))
        num_processes_per_machine = int(_get_key("LOCAL_WORLD_SIZE", 1))
        world_size = int(_get_key("WORLD_SIZE", 1))

        logger.info(
            "Loaded distributed params from os.environ:\n"
            f"    local_rank={local_rank}\n"
            f"    global_rank={global_rank}\n"
            f"    machine_rank={machine_rank}\n"
            f"    num_processes_per_machine={num_processes_per_machine}\n"
            f"    world_size={world_size}\n"
        )

        return DistributedParams(
            local_rank=local_rank,
            global_rank=global_rank,
            machine_rank=machine_rank,
            num_processes_per_machine=num_processes_per_machine,
            world_size=world_size,
        )


@contextlib.contextmanager
def enable_dist_process_groups(
    backend: str,
    init_method: Optional[str],
    dist_params: DistributedParams,
    timeout: timedelta = DEFAULT_TIMEOUT,
):
    assert backend.lower() in ["nccl", "gloo"]
    try:
        dist.init_process_group(
            backend=backend,
            init_method=init_method,
            world_size=dist_params.world_size,
            rank=dist_params.global_rank,
            timeout=timeout,
        )
    except Exception as e:
        logger.error("Process group URL: {}".format(init_method))
        raise e

    if backend.lower() in ["nccl"]:
        torch.cuda.set_device(dist_params.local_rank)
    # synchronize is needed here to prevent a possible timeout after calling
    # init_process_group
    # See: https://github.com/facebookresearch/maskrcnn-benchmark/issues/172
    comm.synchronize()

    with _enable_local_process_group(comm, dist_params):
        yield
    dist.destroy_process_group()


def save_return_deco(return_save_file: Optional[str], rank: int):
    def deco(func):
        """warp a function to save its return to the filename"""

        @functools.wraps(func)
        def new_func(*args, **kwargs):
            ret = func(*args, **kwargs)
            if return_save_file is not None:
                filename = f"{return_save_file}.rank{rank}"
                logger.info(
                    f"Save {func.__module__}.{func.__name__} return to: {filename}"
                )
                torch.save(ret, filename)
            return ret

        return new_func

    return deco


def default_distributed_worker(
    main_func: Callable,
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    backend: str,
    dist_url: Optional[str] = None,
    dist_params: Optional[DistributedParams] = None,
    return_save_file: Optional[str] = None,
    timeout: timedelta = DEFAULT_TIMEOUT,
):
    dist_params = dist_params or DistributedParams.from_environ()
    with enable_dist_process_groups(backend, dist_url, dist_params, timeout):
        deco = save_return_deco(return_save_file, dist_params.global_rank)
        return deco(main_func)(*args, **kwargs)


def launch(
    main_func: Callable,
    num_processes_per_machine: int,
    num_machines: int = 1,
    machine_rank: int = 0,
    dist_url: Optional[str] = None,
    backend: str = "NCCL",
    always_spawn: bool = False,
    launch_method: str = "multiprocessing",
    timeout: timedelta = DEFAULT_TIMEOUT,
    args: Tuple[Any, ...] = (),
    kwargs: Dict[str, Any] = None,
    # NOTE: API of "distributed worker" is not finalized, please reach out if you want
    # to use customized "distributed worker".
    _distributed_worker: Callable = default_distributed_worker,
):
    """Run the `main_func` using multiple processes/nodes
    main_func(*args, **kwargs)
    """

    if kwargs is None:
        kwargs = {}

    if dist_url is None:
        dist_url = f"file:///tmp/mcvdh_dist_file_{time.time()}"

    logger.info(
        f"Launch with num_processes_per_machine: {num_processes_per_machine},"
        f" num_machines: {num_machines}, machine_rank: {machine_rank},"
        f" dist_url: {dist_url}, backend: {backend}."
    )

    if backend == "NCCL":
        assert (
            num_processes_per_machine <= torch.cuda.device_count()
        ), "num_processes_per_machine is greater than device count: {} vs {}".format(
            num_processes_per_machine, torch.cuda.device_count()
        )

    local_ranks = range(
        num_processes_per_machine * machine_rank,
        num_processes_per_machine * (machine_rank + 1),
    )
    world_size = num_machines * num_processes_per_machine
    if world_size > 1 or always_spawn:
        if launch_method not in ["multiprocessing", "elastic"]:
            raise ValueError(f"Invalid launch_method: {launch_method}")
        if launch_method == "elastic":
            lc = pet.LaunchConfig(
                min_nodes=num_machines,
                max_nodes=num_machines,
                nproc_per_node=num_processes_per_machine,
                rdzv_backend="zeus",
                # run_id just has to be globally unique
                run_id=str(hash(dist_url)),  # can't have special character
                # for fault tolerance; set it to 0 for single-node (no fault tolerance)
                max_restarts=3 if num_machines > 1 else 0,
                start_method="spawn",
            )
            results = pet.elastic_launch(lc, entrypoint=_distributed_worker)(
                main_func,
                args,
                kwargs,
                backend,
                None,  # dist_url is not needed for elastic launch
                None,  # no return file is needed
                timeout,
            )
            return results[local_ranks[0]]
        else:
            prefix = f"mcvdh_{main_func.__module__}.{main_func.__name__}_return"
            with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".pth") as f:
                return_file = f.name
                if dist_url.startswith("env://"):
                    # FIXME (tsahi): This branch is not necessary, it doesn't launch
                    # anything, we should simply call distributed_worker_elastic_launch
                    return _distributed_worker(
                        main_func,
                        args,
                        kwargs,
                        backend,
                        dist_url,
                        return_file,  # is this needed?
                        timeout,
                    )
                else:
                    mp.spawn(
                        _mp_spawn_helper,
                        nprocs=num_processes_per_machine,
                        args=(
                            _distributed_worker,
                            main_func,
                            args,
                            kwargs,
                            backend,
                            dist_url,
                            return_file,
                            timeout,
                            world_size,
                            num_processes_per_machine,
                            machine_rank,
                        ),
                        daemon=False,
                    )
                    return torch.load(f"{return_file}.rank{local_ranks[0]}")
    else:
        return main_func(*args, **kwargs)


def _mp_spawn_helper(
    local_rank: int,  # first position required by mp.spawn
    distributed_worker: Callable,
    main_func: Callable,
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    backend: str,
    dist_url: Optional[str],
    return_save_file: Optional[str],
    timeout: timedelta,
    world_size: int,
    num_processes_per_machine: int,
    machine_rank: int,
):
    global_rank = machine_rank * num_processes_per_machine + local_rank
    return distributed_worker(
        main_func=main_func,
        args=args,
        kwargs=kwargs,
        backend=backend,
        dist_url=dist_url,
        dist_params=DistributedParams(
            local_rank=local_rank,
            machine_rank=machine_rank,
            global_rank=global_rank,
            num_processes_per_machine=num_processes_per_machine,
            world_size=world_size,
        ),
        return_save_file=return_save_file,
        timeout=timeout,
    )


@contextlib.contextmanager
def _enable_local_process_group(
    comm_: types.ModuleType,
    dist_params: DistributedParams,
):
    # Setup the local process group (which contains ranks within the same machine)
    assert comm_._LOCAL_PROCESS_GROUP is None
    num_machines = dist_params.world_size // dist_params.num_processes_per_machine
    for i in range(num_machines):
        ranks_on_i = list(
            range(
                i * dist_params.num_processes_per_machine,
                (i + 1) * dist_params.num_processes_per_machine,
            )
        )
        pg = dist.new_group(ranks_on_i)
        if i == dist_params.machine_rank:
            comm_._LOCAL_PROCESS_GROUP = pg

    comm.synchronize()
    yield

    torch.distributed.destroy_process_group(pg)
    comm_._LOCAL_PROCESS_GROUP = None


def launch_deco(
    num_processes: int = 1,
    backend: str = "GLOO",
    always_spawn: bool = True,
    launch_method: str = "multiprocessing",
    timeout: timedelta = DEFAULT_UNITTEST_TIMEOUT,
):
    """
    A helper decorator to run the instance method via `launch`. This is convenient
    to converte a unittest to distributed version.
    """

    def deco(func):
        # use functools.wraps to preserve information like __name__, __doc__, which are
        # very useful for unittest.
        @functools.wraps(func)
        def _launch_func(self, *args, **kwargs):
            ret = launch(
                # make func pickable for the sake of multiprocessing.spawn
                PicklableWrapper(func),
                num_processes_per_machine=num_processes,
                backend=backend,
                always_spawn=always_spawn,
                launch_method=launch_method,
                timeout=timeout,
                # multiprocessing.spawn also requires `args` to be pickable, however
                # the unittest.TestCase instance (i.e. `self`) is not pickable,
                # therefore we also need to wrap it.
                args=(PicklableWrapper(self), *args),
                kwargs=kwargs,
            )
            return ret

        return _launch_func

    return deco


@contextlib.contextmanager
def process_group_with_timeout(timeout, backend=None):
    """
    A helper contextmanager to create a temporary process group using custom timeout
    without changing the global timeout value set by during dist.init_process_group (
    default value is 30 minutes). This is useful when doing heavy communication that the
    default timeout might not be enough.
    """
    pg = torch.distributed.new_group(
        ranks=list(range(comm.get_world_size())),
        timeout=timeout,
        backend=backend,
    )
    yield pg
    torch.distributed.destroy_process_group(pg)
