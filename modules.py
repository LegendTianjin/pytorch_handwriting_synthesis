import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from torch.distributions.distribution import Distribution


class MixtureOfBivariateNormal(Distribution):
    def __init__(self, log_pi, mu, sigma, rho):
        '''
        Mixture of bivariate normal distribution
        Args:
            mu, sigma - (B, T, K, 2)
            rho - (B, T, K)
            log_pi - (B, T, K)
        '''
        super().__init__()
        self.log_pi = log_pi
        self.mu = mu
        self.sigma = sigma
        self.rho = rho

    def log_prob(self, x):
        t = (x - self.mu) / self.sigma
        Z = (t ** 2).sum(-1) - 2 * self.rho * torch.prod(t, -1)

        num = -Z / (2 * (1 - self.rho ** 2))
        denom = np.log(2 * np.pi) + torch.log(self.sigma).sum(-1) + .5 * torch.log(1 - self.rho ** 2)
        log_N = num - denom
        log_prob = torch.logsumexp(self.log_pi + log_N, dim=-1)
        return log_prob

    def sample(self):
        index = self.log_pi.exp().multinomial(1).squeeze(1)
        mu = self.mu[torch.arange(index.shape[0]), index]
        sigma = self.sigma[torch.arange(index.shape[0]), index]
        rho = self.rho[torch.arange(index.shape[0]), index]

        mu1, mu2 = mu.unbind(-1)
        sigma1, sigma2 = sigma.unbind(-1)
        z1 = torch.randn_like(mu1)
        z2 = torch.randn_like(mu2)

        x1 = mu1 + sigma1 * z1
        mult = z2 * ((1 - rho ** 2) ** .5) + z1 * rho
        x2 = mu2 + sigma2 * mult
        return torch.stack([x1, x2], 1)


class SimpleEncoder(nn.Module):
    def __init__(self, vocab_size, emb_size):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, emb_size)

    def forward(self, src, mask):
        return self.emb(src)


class RNNEncoder(nn.Module):
    def __init__(self, vocab_size, emb_size, hidden_size, n_layers):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, emb_size)

        self.rnn = nn.LSTM(
            emb_size, hidden_size, n_layers,
            batch_first=True,
            bidirectional=True,
        )

    def sort(self, src, mask):
        lengths, idx = torch.sort(mask.sum(-1), 0, descending=True)
        return src[idx], lengths, idx

    def unsort(self, src, idx):
        idx = torch.argsort(idx, 0)
        return src[idx]

    def forward(self, src, mask):
        src, lengths, idx = self.sort(src, mask)

        src = self.emb(src)
        src = pack_padded_sequence(src, lengths, batch_first=True)
        out, _ = self.rnn(src)
        out = pad_packed_sequence(out, batch_first=True)[0]

        out = self.unsort(out, idx)
        return out


class GaussianAttention(nn.Module):
    def __init__(self, hidden_size, n_mixtures):
        super().__init__()
        self.n_mixtures = n_mixtures
        self.linear = nn.Linear(hidden_size, 3 * n_mixtures)

    def forward(self, h_t, k_tm1, ctx):
        B, T, _ = ctx.shape

        alpha, beta, kappa = torch.exp(self.linear(h_t))[:, None].chunk(3, dim=-1)  # (B, 1, K) each
        kappa += k_tm1

        u = torch.arange(T, dtype=torch.float32).cuda()
        u = u[None, :, None].repeat(B, 1, 1)  # (B, T, 1)
        phi = alpha * torch.exp(-beta * (kappa - u) ** 2)  # (B, T, K)
        phi = phi.sum(-1, keepdim=True)

        return (phi * ctx).sum(1), phi, kappa


class RNNDecoder(nn.Module):
    def __init__(
        self, enc_size, hidden_size, n_layers,
        n_mixtures_attention, n_mixtures_output
    ):
        super().__init__()
        self.layer_0 = nn.LSTMCell(
            3 + enc_size, hidden_size
        )
        self.layer_n = nn.ModuleList([
            nn.LSTMCell(3 + enc_size + hidden_size, hidden_size)
            for i in range(n_layers - 1)
        ])
        self.attention = GaussianAttention(hidden_size, n_mixtures_attention)
        self.output = nn.Linear(
            hidden_size * n_layers, n_mixtures_output * 6 + 1
        )
        self.h_0 = nn.Parameter(torch.zeros(n_layers, hidden_size))
        self.c_0 = nn.Parameter(torch.zeros(n_layers, hidden_size))
        self.w_0 = nn.Parameter(torch.zeros(enc_size))
        self.k_0 = nn.Parameter(torch.zeros(n_mixtures_attention))

    def forward(self, strokes, context, prev_states=None):
        bsz = strokes.size(0)

        if prev_states is None:
            h_tm1 = list(self.h_0[None].repeat(bsz, 1, 1).unbind(1))
            c_tm1 = list(self.c_0[None].repeat(bsz, 1, 1).unbind(1))
            w_tm1 = self.w_0[None].repeat(bsz, 1)
            k_tm1 = self.k_0[None, None].repeat(bsz, 1, 1)
        else:
            h_tm1, c_tm1, w_tm1, k_tm1 = prev_states

        outputs = []
        for i, x_t in enumerate(strokes.unbind(1)):
            h_tm1[0], c_tm1[0] = self.layer_0(
                torch.cat([x_t, w_tm1], 1),
                (h_tm1[0], c_tm1[0])
            )

            w_tm1, phi, k_tm1 = self.attention(h_tm1[0], k_tm1, context)

            for j, (layer, prev_h, prev_c) in enumerate(zip(self.layer_n, h_tm1[1:], c_tm1[1:])):
                h_tm1[j], c_tm1[j] = layer(
                    torch.cat([x_t, w_tm1, h_tm1[j-1]], 1),
                    (prev_h, prev_c)
                )

            out = self.output(torch.cat(h_tm1, 1))
            outputs.append(out)

        return torch.stack(outputs, 1), (h_tm1, c_tm1, w_tm1, k_tm1)


class Seq2Seq(nn.Module):
    def __init__(
        self, vocab_size, enc_emb_size,
        dec_hidden_size, dec_n_layers,
        n_mixtures_attention, n_mixtures_output
    ):
        super().__init__()
        self.enc = SimpleEncoder(vocab_size, enc_emb_size)
        self.dec = RNNDecoder(
            enc_emb_size, dec_hidden_size, dec_n_layers,
            n_mixtures_attention, n_mixtures_output
        )
        self.n_mixtures_attention = n_mixtures_attention
        self.n_mixtures_output = n_mixtures_output

    def forward(self, strokes, chars, chars_mask):
        K = self.n_mixtures_output

        ctx = self.enc(chars, chars_mask) * chars_mask.unsqueeze(-1)
        out = self.dec(strokes, ctx)[0]

        mu, sigma, pi, rho, eos = out.split([2 * K, 2 * K, K, K, 1], -1)

        sigma = torch.exp(sigma)
        rho = torch.tanh(rho)
        log_pi = F.log_softmax(pi, dim=-1)

        mu = mu.view(mu.shape[:2] + (K, 2))  # (B, T, K, 2)
        sigma = sigma.view(sigma.shape[:2] + (K, 2))  # (B, T, K, 2)

        output_dist = MixtureOfBivariateNormal(log_pi, mu, sigma, rho)
        stroke_loss = -output_dist.log_prob(strokes[:, :, :2].unsqueeze(-2))
        eos_loss = F.binary_cross_entropy_with_logits(eos, strokes[:, :, 2:3])
        return stroke_loss.mean(), eos_loss

    def sample(self, chars, chars_mask, maxlen=1000):
        K = self.n_mixtures_output

        ctx = self.enc(chars, chars_mask) * chars_mask.unsqueeze(-1)
        x_t = torch.zeros(ctx.size(0), 1, 3).float().cuda()
        prev_states = None
        strokes = []
        for i in range(maxlen):
            strokes.append(x_t)
            out, prev_states = self.dec(x_t, ctx, prev_states)

            mu, sigma, pi, rho, eos = out.squeeze(1).split(
                [2 * K, 2 * K, K, K, 1], dim=-1
            )
            sigma = torch.exp(sigma)
            rho = torch.tanh(rho)
            log_pi = F.log_softmax(pi, dim=-1)
            mu = mu.view(-1, K, 2)  # (B, K, 2)
            sigma = sigma.view(-1, K, 2)  # (B, K, 2)

            output_dist = MixtureOfBivariateNormal(log_pi, mu, sigma, rho)
            x_t = torch.cat([
                output_dist.sample(),
                torch.sigmoid(eos).bernoulli(),
            ], dim=1).unsqueeze(1)

        return torch.cat(strokes, 1)


if __name__ == '__main__':
    vocab_size = 60
    emb_size = 128
    hidden_size = 256
    n_layers = 3
    K_att = 10
    K_out = 20

    model = Seq2Seq(vocab_size, emb_size, hidden_size, n_layers, K_att, K_out).cuda()
    chars = torch.randint(0, vocab_size, (16, 50)).cuda()
    chars_mask = torch.ones_like(chars).float()
    strokes = torch.randn(16, 300, 3).cuda()

    loss = model(strokes, chars, chars_mask)
    print(loss)

    out = model.sample(chars, chars_mask)
    print(out.shape)