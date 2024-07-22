from __future__ import annotations

import functools
import itertools
import logging
from typing import Any
from typing import TypeVar
from typing import Union

import accelerate
import datasets
import transformers

logger = logging.getLogger('llm.trainers.gpt')

DatasetT = TypeVar(
    'DatasetT',
    bound=Union[datasets.Dataset, datasets.DatasetDict],
)


def get_datasets(
    *,
    dataset_name: str | None = None,
    dataset_config_name: str | None = None,
    validation_split_percentage: float = 0,
    train_file: str | None = None,
    validation_file: str | None = None,
    keep_linebreaks: bool = True,
) -> datasets.Dataset | datasets.DatasetDict:
    """Get the datasets.

    You can either provide your own CSV/JSON/TXT training and evaluation files
    (see below) or just provide the name of one of the public datasets
    available on the hub at https://huggingface.co/datasets/ (the dataset will
    be downloaded automatically from the datasets Hub).

    For CSV/JSON files, this script will use the column called 'text' or the
    first column if no column called 'text' is found. You can easily tweak this
    behavior (see below).

    In distributed training, the load_dataset function guarantee that only one
    local process can concurrently.
    """
    if dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = datasets.load_dataset(dataset_name, dataset_config_name)
        if 'validation' not in raw_datasets.keys():
            raw_datasets['validation'] = datasets.load_dataset(
                dataset_name,
                dataset_config_name,
                split=f'train[:{validation_split_percentage}%]',
            )
            raw_datasets['train'] = datasets.load_dataset(
                dataset_name,
                dataset_config_name,
                split=f'train[{validation_split_percentage}%:]',
            )
    elif train_file is not None:
        data_files = {}
        dataset_args = {}
        data_files['train'] = train_file
        if validation_file is not None:
            data_files['validation'] = validation_file
        extension = train_file.split('.')[-1]
        if extension == 'txt':
            extension = 'text'
            dataset_args['keep_linebreaks'] = keep_linebreaks
        raw_datasets = datasets.load_dataset(
            extension,
            data_files=data_files,
            **dataset_args,
        )
        # If no validation data is there, validation_split_percentage will be
        # used to divide the dataset.
        if 'validation' not in raw_datasets.keys():
            raw_datasets['validation'] = datasets.load_dataset(
                extension,
                data_files=data_files,
                split=f'train[:{validation_split_percentage}%]',
                **dataset_args,
            )
            raw_datasets['train'] = datasets.load_dataset(
                extension,
                data_files=data_files,
                split=f'train[{validation_split_percentage}%:]',
                **dataset_args,
            )
    else:
        raise ValueError('One of dataset_name or train_file must be provided.')

    return raw_datasets


def preprocess_datasets(
    *,
    raw_datasets: DatasetT,
    tokenizer: transformers.AutoTokenizer,
    accelerator: accelerate.Accelerator,
    num_workers: int | None = None,
    overwrite_cache: bool = False,
    block_size: int | None = None,
) -> DatasetT:
    """Preprocessing the datasets."""
    # First we tokenize all the texts.
    column_names = raw_datasets['train'].column_names
    text_column_name = 'text' if 'text' in column_names else column_names[0]

    def tokenize_function(examples: dict[str, Any]) -> Any:
        return tokenizer(examples[text_column_name])

    with accelerator.main_process_first():
        tokenized_datasets = raw_datasets.map(
            tokenize_function,
            batched=True,
            num_proc=num_workers,
            remove_columns=column_names,
            load_from_cache_file=not overwrite_cache,
            desc='Running tokenizer on dataset',
        )

    if block_size is None:
        block_size = tokenizer.model_max_length
        if block_size > 1024:
            logger.warning(
                'The chosen tokenizer supports a `model_max_length` that is '
                'longer than the default `block_size` value of 1024. If you '
                'would like to use a longer `block_size` up to '
                '`tokenizer.model_max_length` you can override this default '
                'with `--block_size xxx`.',
                extra={'ranks': [0]},
            )
        block_size = 1024
    else:
        if block_size > tokenizer.model_max_length:
            logger.warning(
                f'The block_size passed ({block_size}) is larger than '
                'the maximum length for the model'
                f'({tokenizer.model_max_length}). '
                f'Using block_size={tokenizer.model_max_length}.',
                extra={'ranks': [0]},
            )
        block_size = min(block_size, tokenizer.model_max_length)

    assert isinstance(block_size, int)

    # Note that with `batched=True`, this map processes 1,000 texts together,
    # so group_texts throws away a remainder for each of those groups of
    # 1,000 texts. You can adjust that batch_size here but a higher value
    # might be slower to preprocess.
    #
    # To speed up this part, we use multiprocessing. See the documentation of
    # the map method for more information:
    # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.map  # noqa: E501
    with accelerator.main_process_first():
        lm_datasets = tokenized_datasets.map(
            functools.partial(group_texts, block_size=block_size),
            batched=True,
            num_proc=num_workers,
            load_from_cache_file=not overwrite_cache,
            desc=f'Grouping texts in chunks of {block_size}',
        )

    return lm_datasets


def group_texts(examples: dict[str, Any], block_size: int) -> dict[str, Any]:
    """Concatenates texts from dataset and generates chunks of block_size."""
    # Concatenate all texts.
    concatenated_examples = {
        k: list(itertools.chain(*examples[k])) for k in examples.keys()
    }
    total_length = len(concatenated_examples[next(iter(examples.keys()))])
    # We drop the small remainder, and if the total_length < block_size we
    # exclude this batch and return an empty dict. We could add padding if the
    # model supported it instead of this drop, you can customize this part to
    # your needs.
    total_length = (total_length // block_size) * block_size
    # Split by chunks of max_len.
    result = {
        k: [t[i : i + block_size] for i in range(0, total_length, block_size)]
        for k, t in concatenated_examples.items()
    }
    result['labels'] = result['input_ids'].copy()
    return result
