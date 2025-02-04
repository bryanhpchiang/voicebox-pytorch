import math
import torch
from torch import nn, Tensor, einsum
from torch.nn import Module
import torch.nn.functional as F

from beartype import beartype

from einops import rearrange, repeat, reduce, pack, unpack

from voicebox_pytorch.attend import Attend

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def divisible_by(num, den):
    return (num % den) == 0

def is_odd(n):
    return not divisible_by(n, 2)

# tensor helpers

def prob_mask_like(shape, prob, device):
    if prob == 1:
        return torch.ones(shape, device = device, dtype = torch.bool)
    elif prob == 0:
        return torch.zeros(shape, device = device, dtype = torch.bool)
    else:
        return torch.zeros(shape, device = device).float().uniform_(0, 1) < prob

# sinusoidal positions

class LearnedSinusoidalPosEmb(Module):
    """ used by @crowsonkb """

    def __init__(self, dim):
        super().__init__()
        assert divisible_by(dim, 2)
        half_dim = dim // 2
        self.weights = nn.Parameter(torch.randn(half_dim))

    def forward(self, x):
        x = rearrange(x, 'b -> b 1')
        freqs = x * rearrange(self.weights, 'd -> 1 d') * 2 * math.pi
        fouriered = torch.cat((freqs.sin(), freqs.cos()), dim = -1)
        return fouriered

# rotary positional embeddings
# https://arxiv.org/abs/2104.09864

class RotaryEmbedding(Module):
    def __init__(self, dim, theta = 10000):
        super().__init__()
        inv_freq = 1.0 / (theta ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer("inv_freq", inv_freq)

    @property
    def device(self):
        return self.inv_freq.device

    def forward(self, seq_len):
        t = torch.arange(seq_len, device = self.device).type_as(self.inv_freq)
        freqs = torch.einsum('i , j -> i j', t, self.inv_freq)
        freqs = torch.cat((freqs, freqs), dim = -1)
        return freqs

def rotate_half(x):
    x1, x2 = x.chunk(2, dim = -1)
    return torch.cat((-x2, x1), dim = -1)

def apply_rotary_pos_emb(pos, t):
    return t * pos.cos() + rotate_half(t) * pos.sin()

# convolutional positional generating module

class ConvPositionEmbed(Module):
    def __init__(
        self,
        dim,
        *,
        kernel_size,
        groups = None
    ):
        super().__init__()
        assert is_odd(kernel_size)
        groups = default(groups, dim) # full depthwise conv by default

        self.dw_conv1d = nn.Sequential(
            nn.Conv1d(dim, dim, kernel_size, groups = groups, padding = kernel_size // 2),
            nn.GELU()
        )

    def forward(self, x):
        x = rearrange(x, 'b n c -> b c n')
        x = self.dw_conv1d(x)
        return rearrange(x, 'b c n -> b n c')

# norms

class RMSNorm(Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim = -1) * self.scale * self.gamma

# attention

class Attention(Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8,
        flash = False
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        dim_inner = dim_head * heads

        self.attend = Attend(flash = flash)

        self.norm = RMSNorm(dim)
        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias = False)
        self.to_out = nn.Linear(dim_inner, dim, bias = False)

    def forward(self, x, mask = None, rotary_emb = None):
        h = self.heads

        x = self.norm(x)

        q, k, v = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))

        if exists(rotary_emb):
            q, k = map(lambda t: apply_rotary_pos_emb(t, rotary_emb), (q, k))

        out = self.attend(q, k, v, mask = mask)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# feedforward

def FeedForward(dim, mult = 4):
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim * mult),
        nn.GELU(),
        nn.Linear(dim * mult, dim)
    )

# transformer

class Transformer(Module):
    def __init__(
        self,
        dim,
        *,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        attn_flash = False
    ):
        super().__init__()
        assert divisible_by(depth, 2)

        self.layers = nn.ModuleList([])

        self.rotary_emb = RotaryEmbedding(dim = dim_head)

        for ind in range(depth):
            layer = ind + 1
            has_skip = layer > (depth // 2)

            self.layers.append(nn.ModuleList([
                nn.Linear(dim * 2, dim) if has_skip else None,
                Attention(dim = dim, dim_head = dim_head, heads = heads, flash = attn_flash),
                FeedForward(dim = dim, mult = ff_mult)
            ]))

    def forward(self, x):
        skip_connects = []

        rotary_emb = self.rotary_emb(x.shape[-2])

        for skip_combiner, attn, ff in self.layers:

            # in the paper, they use a u-net like skip connection
            # unclear how much this helps, as no ablations or further numbers given besides a brief one-two sentence mention

            if not exists(skip_combiner):
                skip_connects.append(x)
            else:
                x = torch.cat((x, skip_connects.pop()), dim = -1)
                x = skip_combiner(x)

            x = attn(x, rotary_emb = rotary_emb) + x
            x = ff(x) + x

        return x

# both duration and main denoising model are transformers

class DurationPredictor(Module):
    def __init__(
        self,
        *,
        num_phoneme_tokens,
        dim_phoneme_emb = 512,
        dim = 512,
        depth = 10,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        conv_pos_embed_kernel_size = 31,
        conv_pos_embed_groups = None,
        attn_flash = False
    ):
        super().__init__()

        self.null_phoneme_id = num_phoneme_tokens # use last phoneme token as null token for CFG
        self.to_phoneme_emb = nn.Embedding(num_phoneme_tokens + 1, dim_phoneme_emb)

        self.to_embed = nn.Linear(dim * 2 + dim_phoneme_emb, dim)

        self.null_cond = nn.Parameter(torch.zeros(dim))

        self.conv_embed = ConvPositionEmbed(
            dim = dim,
            kernel_size = conv_pos_embed_kernel_size,
            groups = conv_pos_embed_groups
        )

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            dim_head = dim_head,
            heads = heads,
            ff_mult = ff_mult,
            attn_flash = attn_flash
        )

    @torch.inference_mode()
    def forward_with_cond_scale(
        self,
        *args,
        cond_scale = 1.,
        **kwargs
    ):
        logits = self.forward(*args, cond_drop_prob = 0., **kwargs)

        if cond_scale == 1.:
            return logits

        null_logits = self.forward(*args, cond_drop_prob = 1., **kwargs)
        return null_logits + (logits - null_logits) * cond_scale

    def forward(
        self,
        x,
        *,
        phoneme_ids,
        cond,
        cond_drop_prob = 0.,
        target = None,
        mask = None
    ):
        assert cond.shape[-1] == x.shape[-1]

        # classifier free guidance

        if cond_drop_prob > 0.:
            cond_drop_mask = prob_mask_like(cond.shape[:1], cond_drop_prob, cond.device)

            cond = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1 1'),
                self.null_cond,
                cond
            )

            phoneme_ids = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1'),
                self.null_phoneme_id,
                phoneme_ids
            )

        phoneme_emb = self.to_phoneme_emb(phoneme_ids)

        # combine audio, phoneme, conditioning

        embed = torch.cat((x, phoneme_emb, cond), dim = -1)
        x = self.to_embed(embed)

        x = self.conv_embed(x) + x
        x = self.transformer(x)

        if not exists(mask):
            return F.l1_loss(x, target)

        loss = F.l1_loss(x, target, reduction = 'none')

        if exists(mask):
            loss = reduce(loss, 'b n d -> b n', 'mean')
            loss = loss.masked_fill(mask, 0.)

            # masked mean

            num = reduce(loss, 'b n -> b', 'sum')
            den = mask.sum(dim = -1).clamp(min = 1e-5)
            loss = num / den

        return loss.mean()


class VoiceBox(Module):
    def __init__(
        self,
        *,
        num_phoneme_tokens,
        dim_phoneme_emb = 1024,
        dim = 1024,
        depth = 24,
        dim_head = 64,
        heads = 16,
        ff_mult = 4,
        conv_pos_embed_kernel_size = 31,
        conv_pos_embed_groups = None,
        attn_flash = False
    ):
        super().__init__()
        self.sinu_pos_emb = LearnedSinusoidalPosEmb(dim)

        self.null_phoneme_id = num_phoneme_tokens # use last phoneme token as null token for CFG
        self.to_phoneme_emb = nn.Embedding(num_phoneme_tokens + 1, dim_phoneme_emb)

        self.to_embed = nn.Linear(dim * 2 + dim_phoneme_emb, dim)

        self.null_cond = nn.Parameter(torch.zeros(dim))

        self.conv_embed = ConvPositionEmbed(
            dim = dim,
            kernel_size = conv_pos_embed_kernel_size,
            groups = conv_pos_embed_groups
        )

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            dim_head = dim_head,
            heads = heads,
            ff_mult = ff_mult,
            attn_flash = attn_flash
        )

    @torch.inference_mode()
    def forward_with_cond_scale(
        self,
        *args,
        cond_scale = 1.,
        **kwargs
    ):
        logits = self.forward(*args, cond_drop_prob = 0., **kwargs)

        if cond_scale == 1.:
            return logits

        null_logits = self.forward(*args, cond_drop_prob = 1., **kwargs)
        return null_logits + (logits - null_logits) * cond_scale

    def forward(
        self,
        x,
        *,
        phoneme_ids,
        cond,
        times,
        cond_drop_prob = 0.1,
        target = None,
        mask = None,
    ):
        assert cond.shape[-1] == x.shape[-1]

        # classifier free guidance

        if cond_drop_prob > 0.:
            cond_drop_mask = prob_mask_like(cond.shape[:1], cond_drop_prob, cond.device)

            cond = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1 1'),
                self.null_cond,
                cond
            )

            phoneme_ids = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1'),
                self.null_phoneme_id,
                phoneme_ids
            )

        phoneme_emb = self.to_phoneme_emb(phoneme_ids)
        embed = torch.cat((x, phoneme_emb, cond), dim = -1)
        x = self.to_embed(embed)

        x = self.conv_embed(x) + x

        # add sinusoidal time embedding along time axis

        time_emb = self.sinu_pos_emb(times)
        x, ps = pack((time_emb, x), 'b * d')

        # attend

        x = self.transformer(x)

        # split out time embedding

        _, x = unpack(x, ps, 'b * d')

        # if no target passed in, just return logits

        if not exists(target):
            return x

        if not exists(mask):
            return F.mse_loss(x, target)

        loss = F.mse_loss(x, target, reduction = 'none')

        if exists(mask):
            loss = reduce(loss, 'b n d -> b n', 'mean')
            loss = loss.masked_fill(mask, 0.)

            # masked mean

            num = reduce(loss, 'b n -> b', 'sum')
            den = mask.sum(dim = -1).clamp(min = 1e-5)
            loss = num / den

        return loss.mean()

# wrapper for the CNF

class CNFWrapper(Module):
    @beartype
    def __init__(
        self,
        voicebox: VoiceBox
    ):
        super().__init__()

    def forward(self, x):
        return x
