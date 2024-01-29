"""RoBERTa pretraining config.

Pretrains a BERT-Large model using the RoBERTa procedure. The default
parameters here correspond to the "RoBERTa with Books + Wiki" config from
Table 4 in the paper https://arxiv.org/pdf/1907.11692.pdf. You can increase
STEPS to 500,000 to replicate the "pretraing even longer" results.

Notes:
  - The Adam betas used in the paper are slightly different that those used
    here.
  - Gradient clipping is used because we use mixed precision here.
"""

from __future__ import annotations

import os

import torch

from llm.models import bert as bert_models
from llm.trainers.bert.data import RobertaDatasetConfig

OUTPUT_DIR = 'runs/roberta-large-pretraining'
CHECKPOINT_DIR = os.path.join(OUTPUT_DIR, 'checkpoints/')
TENSORBOARD_DIR = os.path.join(OUTPUT_DIR, 'tensorboard/')
LOG_FILE = os.path.join(OUTPUT_DIR, 'logs/pretraining.txt')
DATASET_CONFIG = RobertaDatasetConfig(
    input_dir='/grand/SuperBERT/jgpaul/datasets/encoded/wikibooks/',
    tokenizer_file='/grand/SuperBERT/jgpaul/datasets/tokenizers/tokenzer.json',
    mask_token_prob=0.15,
)

# RoBERTa has only a single phase of training
PHASE = 1
_config = bert_models.BERT_LARGE
_config['vocab_size'] = 50000
BERT_CONFIG = _config
OPTIMIZER = 'adam'
GRADIENT_CHECKPOINTING = False
DTYPE = torch.float16
SEED = 42

# ACCUMULATION_STEPS is computed automatically by llm.trainers.bert
LR = 4e-4
CLIP_GRAD_NORM = 1.0
WARMUP_STEPS = 30000
STEPS = 100000
GLOBAL_BATCH_SIZE = 8192
BATCH_SIZE = 8
CHECKPOINT_STEPS = 1000
