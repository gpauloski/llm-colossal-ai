"""Utilities for training with sharded datasets."""

from __future__ import annotations

import random
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sized
from typing import Any
from typing import TypeVar

import torch
from torch.utils.data import Dataset

SampleType = TypeVar('SampleType', covariant=True)
DatasetParams = tuple[tuple[Any, ...], Mapping[str, Any]]


class DistributedShardedDataset(Dataset[SampleType]):
    """Dataset wrapper for sharded datasets in distributed environments.

    This class manages a set of datasets (shards) and
    restricts ranks to viewing a subset of the global indices across the
    shards. This is achieved by sorting the shards and counting the samples
    in each shard to compute the total number of samples then chunking those
    samples by rank.

    For example, if there are four ranks and eight shards of equal size, rank
    zero will see shards zero and one, rank two will see shards two and three,
    and so on. The length of an instance of this class as seen by a rank
    will be `(1 / world_size) * sum_of_samples_across_shards`.

    This class also ensures only one shard is loaded at a time
    on a rank so the full dataset is never loaded into memory at once.

    Warning:
        When building a [`DataLoader`][torch.utils.data.DataLoader] from a
        [`DistributedShardedDataset`][llm.datasets.sharded.DistributedShardedDataset],
        do NOT use PyTorch's
        [`DistributedSampler`][torch.utils.data.distributed.DistributedSampler].
        If you want to be able to save the state of the data loader, use the
        [`SequentialSampler`][torch.utils.data.SequentialSampler] because this
        class already provides the support for partitioning samples across
        ranks. This module provides a
        [`ResumableSequentialSampler`][llm.datasets.sharded.ResumableSequentialSampler]
        to enable resuming sampling from the last sampled index.

    Note:
        Samples at the end of the last shard will be dropped to ensure
        each rank sees an equal number of samples.

    Todo:
        * Next shard prefetching
        * Sample index shuffling within a shard
        * Support shuffle shard order by epoch

    Args:
        dataset_type: Dataset type that represents a single shard. This
            subtype of Dataset must be a map-style dataset. Iterable-style
            datasets are not supported.
        shard_params: Dictionary mapping shard keys to the parameters used
            to initialize a `dataset_type` for the shard. The parameter type
            is a tuple of args and kwargs.
        rank: Rank of this process.
        world_size: Number of ranks sharing the dataset.
        shuffle: Shuffle the shard order by the shard keys. The default
            (`False`) sorts the shards by shard key.
        seed: Seed used for shuffling the shard order.
    """

    def __init__(
        self,
        dataset_type: type[Dataset[SampleType]],
        shard_params: dict[str, DatasetParams],
        *,
        rank: int,
        world_size: int,
        shuffle: bool = False,
        seed: int = 0,
    ) -> None:
        if not (0 <= rank < world_size):
            raise ValueError(
                f'Got rank={rank} which does not satisfy 0 <= rank < '
                f'world_size where world_size={world_size}.',
            )
        if len(shard_params) == 0:
            raise ValueError(
                'Parameters for at least one shard must be provided.',
            )

        random.seed(seed)

        self.dataset_type = dataset_type
        self.shard_params = shard_params
        self.rank = rank
        self.world_size = world_size
        self.shuffle = shuffle

        shard_keys = sorted(shard_params.keys())
        if shuffle:
            random.shuffle(shard_keys)

        # Mapping of shard_key to (start_index, end_index)
        shard_indices: dict[str, tuple[int, int]] = {}
        index = 0
        for shard_key in shard_keys:
            shard = self.load_shard(shard_key)
            assert isinstance(shard, Sized)
            shard_indices[shard_key] = (index, index + len(shard))
            index += len(shard)
            del shard

        # Drop indices from last shard to make divisible by world size
        last_shard_key = shard_keys[-1]
        last_shard_indices = shard_indices[last_shard_key]
        shard_indices[last_shard_key] = (
            last_shard_indices[0],
            last_shard_indices[1] - (last_shard_indices[1] % world_size),
        )

        self.shard_keys = shard_keys
        self.shard_indices = shard_indices
        self.total_samples = shard_indices[last_shard_key][1]

        assert len(shard_keys) == len(shard_indices) == len(shard_params)
        assert len(self) * self.world_size == self.total_samples

        self._current_shard_key: str | None = None
        self._current_shard: Dataset[SampleType] | None = None

    def __len__(self) -> int:
        return self.total_samples // self.world_size

    def __getitem__(self, rank_index: int) -> SampleType:
        if rank_index >= len(self):
            raise IndexError(
                f'Requested sample index {rank_index} exceeds dataset size of'
                f'{len(self)} samples.',
            )
        shard_key, shard_index = self.rank_index_to_shard_index(rank_index)

        if (
            self._current_shard_key is None
            or self._current_shard_key != shard_key
        ):
            self._current_shard_key = shard_key
            self._current_shard = self.load_shard(shard_key)

        # If self._current_shard_key is not None then self._current_shard
        # should never be None.
        assert self._current_shard is not None

        return self._current_shard[shard_index]

    def rank_index_to_global_index(self, rank_index: int) -> int:
        """Convert an index local to a rank to a global index."""
        rank_start_index = len(self) * self.rank
        return rank_start_index + rank_index

    def rank_index_to_shard_index(self, rank_index: int) -> tuple[str, int]:
        """Convert an index local to a rank to a shard and shard index.

        Args:
            rank_index: Dataset index local to the rank.

        Returns:
            Tuple of the shard key and the index within the shard that \
            `rank_index` corresponds to.
        """
        global_index = self.rank_index_to_global_index(rank_index)
        for shard_key in self.shard_keys:
            shard_indices = self.shard_indices[shard_key]
            if shard_indices[0] <= global_index < shard_indices[1]:
                return (shard_key, global_index - shard_indices[0])
        raise AssertionError(
            f'Rank index {rank_index} for rank {self.rank} maps to global '
            f'index {global_index} which exceeds the total samples in the '
            f'dataset ({self.total_samples}).',
        )

    def load_shard(self, shard_key: str) -> Dataset[SampleType]:
        args, kwargs = self.shard_params[shard_key]
        return self.dataset_type(*args, **kwargs)


class ResumableSequentialSampler(torch.utils.data.Sampler[int]):
    """Resumable sequential sampler.

    Args:
        data_source: Dataset to sample sequentially from.
        start_index: Index to resume sequential sampling from.
    """

    def __init__(self, data_source: Sized, start_index: int = 0) -> None:
        self.data_length = len(data_source)
        self.start_index = start_index
        self.index = start_index

    def __iter__(self) -> Iterator[int]:
        while self.index < self.data_length:
            yield self.index
            self.index += 1

    def __len__(self) -> int:
        return self.data_length - self.start_index
