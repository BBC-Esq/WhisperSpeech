# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/2B. Whisper quantization (semantic token) model.ipynb.

# %% auto 0
__all__ = ['RQBottleneckTransformer', 'make_model']

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 2
import io
import sys
import time
import torch
import torchaudio

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 3
from pathlib import Path
import json
from fastprogress import progress_bar, master_bar
import fastprogress
import numpy as np
import pylab as plt
import pandas as pd
import random

import whisper
import whisper.tokenizer
from huggingface_hub import hf_hub_download
from fastcore.basics import store_attr

from torch import nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader
import webdataset as wds
from . import utils, vad_merge

from vector_quantize_pytorch import ResidualVQ

from fastcore.script import *

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 13
def add_masks(samples):
    for s in samples:
        seconds = s['tend'] - s['tstart']
        sr = 16000 #s['sample_rate']
        # a mask (downsampled to the Whisper encoder token rate of 50/s) is used
        # to teach the model the concept of padding
        # this let's us decode shorter sequences later
        mask = torch.zeros(30*sr//320, dtype=torch.bool)
        mask[:int(seconds * sr) // 320] = 1
        s['mask'] = mask
        yield s
        
def get_tokenizer(model, language):
    multilingual = not model.endswith(".en")
    return whisper.tokenizer.get_tokenizer(multilingual, language=language, task="transcribe",
                                           num_languages = 100 if model == 'large-v3' else 99)

def tokenize_text(samples, ttoks_size=200, model="base.en", language="en"):
    tokenizer = get_tokenizer(model, language)
    for s in samples:
        ttoks = tokenizer.encode(s['txt'])
        tokens = list(tokenizer.sot_sequence_including_notimestamps) + ttoks
        rpad = ttoks_size - len(tokens)
        s['in_ttoks'] = F.pad(torch.tensor(tokens), (0, rpad), value=tokenizer.eot)
        s['out_ttoks'] = F.pad(torch.tensor(tokens[1:] + [tokenizer.eot]), (0, rpad), value=-100)
        yield s

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 14
def load_dataset(
        dataset_dir:Path,
        txt_label:str="base.en-txt", # the label of the files containing transcriptions
        model:str="base.en",
        language:str=None,
        weight:float=1,
        validation:bool=False,
        exclude_datasets:str="txt-random-valid", # space separated directory names for validation datasets to exclude
    ):
    dataset_dir = Path(dataset_dir)
    shards = utils.shard_glob(dataset_dir/'audio/*.tar')
    with open(dataset_dir/'txt-samples.list') as f: samples = len(f.readlines())
    language = utils.readlines(dataset_dir/'language')[0]
    
    txt_dir = None
    for name in ['small.en-txt', 'medium-txt']:
        if (dataset_dir/name).exists():
            txt_dir = name 
            break
    if txt_dir is None: raise ArgumentError(f"No transcripts found in {dataset_dir}")

    excludes = {x
                for dir in exclude_datasets.split()
                for x in utils.readlines(dataset_dir/Path(dir)/"txt-samples.list")
               } if not validation and exclude_datasets else set()
    
    if not language and model.endswith('en'): language = 'en'
    assert language, "please provide the dataset language for multilang models"
    
    same_on_all_nodes = lambda urls: urls # will only be used for validation
    ds = vad_merge.chunked_audio_dataset(shards, 'raw',
                                         resampled=not validation, nodesplitter=same_on_all_nodes).compose(
        utils.merge_in(utils.derived_dataset(txt_dir)),
        wds.select(lambda s: s['__key__'] not in excludes),
        utils.resampler(16000, 'samples_16k'),
        add_masks,
        lambda x: tokenize_text(x, model=model, language=language),
        wds.to_tuple('samples_16k', 'mask', 'in_ttoks', 'out_ttoks'),
    )
    if not validation:
        ds = ds.compose(wds.shuffle(500, initial=500))
    ds = ds.batched(64)
    if validation:
        ds = ds.slice(samples // 64)
    ds.total_samples = samples
    ds.weight = weight
    
    return ds

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 22
from whisperspeech.train import *
from whisperspeech.modules import *

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 23
import dataclasses

def rand(start, end):
    return random.random() * (end - start) + start

def logrand(start, end):
    return 10**rand(math.log10(start), math.log10(end))

@dataclasses.dataclass
class Tunables:
    init_std :float = 1.5
    embeddings_std :float = 4.5e-2
    embeddings_lr_scale: float = 1
    query_mult :float = 2
    rope :bool = True
    mask_embs :bool = True # force embeddings corresponding to the input audio padding to a constant value
    downsample_conv: bool = False
    downsample_mean: bool = True
        
    codebook_dim: int = 32 # FIXME: unused
    codebook_decay: float = 0.9
    
    lr0 :float = .9e-3
    clip_gradient_norm :float = 2
    weight_decay :float = 1e-3
    warmup_steps :float = 850

    random :bool = False

    # unused, for backwards compatibility:
    output_mult :int = None

    def __post_init__(self):
        # randomize the hyperparams if requested
        if self.random:
            self.init_std = logrand(1, 2)
            self.embeddings_std = logrand(3e-2,6e-2)
            self.embeddings_lr_scale = 2**rand(0,3)
            self.query_mult = logrand(1,8)
            self.codebook_dim = int(logrand(30,50))
            self.codebook_decay = logrand(0.86,0.95)
            self.rope = True
            self.mask_embs = True
            self.downsample_mean = True
            
            self.lr0 = logrand(.8e-3,1e-3)
            self.clip_gradient_norm = 10**rand(-1,1)
            self.warmup_steps = logrand(700,1000)
            
    @staticmethod
    def upgrade(args):
        args = {k:v for k,v in args.items()}
        def old_default(name, value):
            if name not in args: args[name] = value
        old_default('output_mult', 1)
        old_default('query_mult', 1)
        old_default('rope', False)
        old_default('mask_embs', False)
        old_default('downsample_conv', False)
        old_default('downsample_mean', False)
        if 'encoder_depth_ratio' in args: del args['encoder_depth_ratio']
        if 'vq_codes' in args: del args['vq_codes']
        return args

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 24
import math

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 25
class RQBottleneckTransformer(nn.Module):
    def __init__(self, vq_codes=512, q_depth=12, depth=1, n_head=2, head_width=64, ffn_mult=4,
                 codebook_dim=2, threshold_ema_dead_code=2, use_cosine_sim = False, kl_loss_mul=1,
                 downsample=1, no_quantize=False,
                 whisper_model_name='tiny.en', tunables=Tunables()):
        super().__init__()
        width = n_head * head_width
        store_attr("codebook_dim,vq_codes,q_depth,n_head,head_width,ffn_mult,depth,use_cosine_sim,downsample,whisper_model_name")
        self.width = width
        self.base_width = 3 * head_width
        self.vq_codes = vq_codes
        self.tunables = tunables
        self.stoks_len = 1500//downsample
        self.stoks_per_sec = self.stoks_len//30
        self.no_quantize = no_quantize
                
        qk_scale = self.tunables.query_mult * 8 / math.sqrt(head_width)
        
        self.kl_loss_mul = kl_loss_mul
        
        if no_quantize:
            # a mode to get Whisper baselines into W&B easily, skips all training
            self.fake_parameter = nn.Parameter(torch.tensor(0.001))
        else:
            n_mlp = width * ffn_mult
            self.mlp = nn.Sequential(
                nn.Linear(width, n_mlp), nn.GELU(), nn.Linear(n_mlp, width)
            )
            self.mlp_ln = LayerNorm(width)

            if tunables.downsample_conv:
                self.downsample_conv = nn.Conv1d(width, width, kernel_size=3, stride=downsample, padding=1)
            else:
                self.downsample_conv = None

            if tunables.mask_embs: vq_codes = vq_codes + 1
            self.rq = ResidualVQ(
                dim = width,
                codebook_size = vq_codes, # codebook size
                decay = tunables.codebook_decay, # the exponential moving average decay, lower means the dictionary will change faster
                commitment_weight = 1.,   # the weight on the commitment loss
                threshold_ema_dead_code = threshold_ema_dead_code,
                use_cosine_sim = use_cosine_sim,
                codebook_dim = codebook_dim,
                num_quantizers= 1,
            )

            self.positional_embedding = nn.Embedding(1500, width) # FIXME: should be self.stoks_len

            self._out_blocks = nn.Sequential(*[
                ResidualAttentionBlock(width, n_head, qk_scale=qk_scale, ffn_mult=ffn_mult, rope=tunables.rope) for _ in range(depth)
            ])
            self.ln_post = LayerNorm(width)

        self.positions = torch.arange(0, 1500, dtype=torch.long)
        
        self.ce_lossf = nn.CrossEntropyLoss(ignore_index=-100)
        self.kl_lossf = nn.KLDivLoss(reduction='batchmean')
                
        self.whmodel = None

        self.apply(self.init_transformer)
        self.register_buffer('val_true', torch.zeros(1))
        self.register_buffer('val_total', torch.zeros(1))
    
    def setup(self, device):
        self.ensure_whisper(device)
    
    def init_transformer(self, m):
        if isinstance(m, LinearHead):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, QueryHead):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, nn.Embedding):
            m.no_weight_decay = True
            m.lr_scale = self.tunables.embeddings_lr_scale
            std = self.tunables.embeddings_std
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.Linear):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            std = self.tunables.init_std / m.weight.shape[1]
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
            if m.bias is not None:
                torch.nn.init.trunc_normal_(m.bias, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.LayerNorm):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.bias, 0)
            torch.nn.init.constant_(m.weight, 1)

    @property
    def device(self):
        return next(self.parameters()).device
            
    #
    # training
    #
    def log_mel_spectrogram(self, samples):
        return whisper.log_mel_spectrogram(samples, 128 if self.whisper_model_name == 'large-v3' else 80)
    
    @torch.no_grad()
    def extract_teacher(self, samples, input_toks, output_toks):
        embs = self.whmodel[0].encoder(self.log_mel_spectrogram(samples))
        teacher_logits = self.whmodel[0].decoder(input_toks, embs)
        # set teacher logits to 0 for padding positions so KLDivLoss ignores them
        teacher_logits[output_toks == -100] = 0
        return embs, teacher_logits
    
    def downsample_embeddings(self, x):
        if self.downsample_conv is not None:
            return x[:,::self.downsample] + self.downsample_conv(x.transpose(-1,-2)).transpose(-2,-1)
        elif self.tunables.downsample_mean:
            bs,slen,depth = x.shape
            return x.reshape(bs,slen//self.downsample,self.downsample,depth).mean(-2)
        else:
            return x[:,::self.downsample]
        
    def out_blocks(self, x):
        for l in self._out_blocks: x = l(x, self.positions)
        return x
    
    def forward(self, samples, mask, input_toks, output_toks):
        embs, teacher_logits = self.extract_teacher(samples, input_toks, output_toks)
        
        if not self.no_quantize:
            x = self.downsample_embeddings(embs)
            x = x + self.mlp(self.mlp_ln(x))
            # VQ bottleneck
            quantized, self.indices, self.commit_loss = self.rq(x)
            self.commit_loss = self.commit_loss.mean()

            x = quantized.repeat_interleave(self.downsample, -2)
            project_out = getattr(self.rq, 'project_out', None) or self.rq.layers[0].project_out
            if self.tunables.mask_embs: x[~mask] = project_out(self.rq.layers[0]._codebook.embed[0,self.vq_codes])
            x = x + self.positional_embedding(self.positions.to(x.device))
            x = self.ln_post(self.out_blocks(x))
        
        logits = self.whmodel[0].decoder(input_toks, embs if self.no_quantize else x)
        self.ce_loss = self.ce_lossf(logits.view(-1,logits.shape[-1]), output_toks.view(-1))
        self.kl_loss = self.kl_lossf(F.log_softmax(logits, dim=-1), F.softmax(teacher_logits, dim=-1))
        loss = self.ce_loss + self.kl_loss_mul * self.kl_loss
        if not self.no_quantize: loss += self.commit_loss
        x = None
        if self.no_quantize: loss = loss + self.fake_parameter
        
        if not self.training:
            valid_toks = output_toks != -100
            self.val_true += (logits.detach().argmax(-1)[valid_toks] == output_toks[valid_toks]).float().sum()
            self.val_total += valid_toks.float().sum()

        return x, logits, loss

    def get_metrics(self):
        metrics = {
            'acc_0': (self.val_true / self.val_total).item(),
        }
        self.val_true[:] = 0
        self.val_total[:] = 0
        return metrics
    
    #
    # inference
    #
    @classmethod
    def load_model(cls, ref="collabora/spear-tts-pytorch:whisper-vq-stoks-medium-en+pl.model",
                   repo_id=None, filename=None, local_filename=None, cache_dir=None):
        if repo_id is None and filename is None and local_filename is None:
            if ":" in ref:
                repo_id, filename = ref.split(":", 1)
            else:
                local_filename = ref
        if not local_filename:
            local_filename = hf_hub_download(repo_id=repo_id, filename=filename, cache_dir=cache_dir)
        spec = torch.load(local_filename) 
        vqmodel = cls(**spec['config'], tunables=Tunables(**Tunables.upgrade(spec.get('tunables', {}))))
        vqmodel.load_state_dict(spec['state_dict'])
        vqmodel.eval()
        return vqmodel
    
    def load_checkpoint(self, local_filename):
        spec = torch.load(local_filename, map_location='cpu')
        assert 'pytorch-lightning_version' in spec, 'not a valid PyTorch Lightning checkpoint'
        state_dict = {k.replace('model.', ''):v
                      for k,v in spec['state_dict'].items()}
        self.load_state_dict(state_dict)
        return self
    
    def save_model(self, fname, store_parameters=True):
        torch.save(dict(config = self.__stored_args__,
                        tunables = dataclasses.asdict(self.tunables),
                        state_dict = self.state_dict() if store_parameters else None), fname)
        
    def ensure_whisper(self, device=None):
        if self.whmodel is not None: return
        device = device or self.device
        # the list wrapper is a hack to make sure the whole of Whisper is not sucked into self.parameters()
        if self.whmodel is None: self.whmodel = [whisper.load_model(self.whisper_model_name, device=device)]
        self.decoding_options = whisper.DecodingOptions()
        self.tokenizer = get_tokenizer(self.whisper_model_name, None)
    
    def quantize(self, embs):
        x = self.downsample_embeddings(embs)
        x = x + self.mlp(self.mlp_ln(x))
        _, stoks, _ = self.rq(x)
        if self.q_depth == 1:
            stoks = stoks.squeeze(-1)
        return stoks

    def dequantize(self, stoks):
        assert self.q_depth == 1
        assert len(stoks.shape) == 1, "batch processing is not supported"
        if isinstance(stoks, np.ndarray): stoks = torch.tensor(stoks)
        # remove padding
        padding = torch.nonzero(stoks == self.vq_codes)
        if padding.any(): stoks = stoks[:padding[0,0]]
        stoks = F.pad(stoks, (0,self.stoks_len - stoks.shape[-1]), value=self.vq_codes if self.tunables.mask_embs else 0)
        x = self.rq.layers[0]._codebook.embed[0,stoks.to(torch.long).view(-1)]
        x = x.repeat_interleave(self.downsample, -2)
        project_out = getattr(self.rq, 'project_out', None) or self.rq.layers[0].project_out
        x = project_out(x).unsqueeze(0)
        positions = torch.arange(0, x.shape[-2], dtype=torch.long, device=x.device)
        x = x + self.positional_embedding(positions)
        return self.ln_post(self.out_blocks(x))

    def encode_audio(self, audio):
        if isinstance(audio, str):
            x, sr = torchaudio.load(audio)
            x = torchaudio.transforms.Resample(sr, 16000)(x)[0]
            audio = x.unsqueeze(0)
        return self.encode_mel(self.log_mel_spectrogram(audio).to(self.device))
    
    def encode_mel(self, mel):
        assert len(mel.shape) == 3, "invalid mel spectrogram shape, expect (batch,chn,time)"
        self.ensure_whisper()
        n = mel.shape[-1]
        if n > whisper.audio.N_FRAMES:
            padding = 0
            padded = mel[:,:,:whisper.audio.N_FRAMES]
        else:
            padding = -n % whisper.audio.N_FRAMES
            padded = F.pad(mel, (0, padding), value=-1.5)
        embs = self.whmodel[0].encoder(padded)#.to(self.whmodel[0].device))#[:,:n//2]
        stoks = self.quantize(embs)
        if self.tunables.mask_embs:
            return stoks[:,:n//2//self.downsample]
        else:
            return stoks
    
    def decode_text(self, stoks, decoding_options=None):
        self.ensure_whisper(self.device)
        if decoding_options is None: decoding_options = self.decoding_options
        embs = self.dequantize(stoks).to(self.whmodel[0].device)
        return self.whmodel[0].decode(embs, decoding_options)

# %% ../nbs/2B. Whisper quantization (semantic token) model.ipynb 27
def make_model(size:str, no_quantize=False, tunables:Tunables=Tunables(), dataset:torch.utils.data.Dataset=None):
    common = dict(
        q_depth=1, depth=1, threshold_ema_dead_code=0, use_cosine_sim=True, tunables=tunables,
        no_quantize = no_quantize,
    )
    if size == 'base.en-2d-4096c':
        model = RQBottleneckTransformer(codebook_dim=32, vq_codes=4096, n_head=8, downsample=2, 
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'base.en-2d-512c':
        model = RQBottleneckTransformer(codebook_dim=32, vq_codes=512, n_head=8, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'base.en-2d-512c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=512, n_head=8, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'base-2d-512c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=512, n_head=8, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'base-2d-1024c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=1024, n_head=8, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-256c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=256, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-256c-dim128':
        model = RQBottleneckTransformer(codebook_dim=128, vq_codes=256, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-512c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=512, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-512c-dim128':
        model = RQBottleneckTransformer(codebook_dim=128, vq_codes=512, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-512c-dim256':
        model = RQBottleneckTransformer(codebook_dim=256, vq_codes=512, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-1024c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=1024, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'medium-2d-2048c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=2048, n_head=16, downsample=2,
                                        whisper_model_name=size.split("-")[0], **common)
        return model
    if size == 'large-v2-2d-512c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=512, n_head=20, downsample=2,
                                        whisper_model_name='large-v2', **common)
        return model
    if size == 'large-v3-2d-512c-dim64':
        model = RQBottleneckTransformer(codebook_dim=64, vq_codes=512, n_head=20, downsample=2,
                                        whisper_model_name='large-v3', **common)
        return model
    raise ArgumentError(f"invalid model size: {size}")
