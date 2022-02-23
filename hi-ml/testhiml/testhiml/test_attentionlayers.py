import pytest
from typing import Type, Union

from torch import nn, rand, sum, allclose, ones_like

from health_ml.networks.layers.attention_layers import (AttentionLayer, GatedAttentionLayer,
                                                        MeanPoolingLayer)


def _test_attention_layer(attentionlayer: nn.Module, dim_in: int, dim_att: int,
                          batch_size: int,) -> None:
    features = rand(batch_size, dim_in)                   # N x L x 1 x 1
    attn_weights, output_features = attentionlayer(features)
    assert attn_weights.shape == (dim_att, batch_size)          # K x N
    assert output_features.shape == (dim_att, dim_in)           # K x L
    assert ((attn_weights >= 0) & (attn_weights <= 1)).all()

    row_sums = sum(attn_weights, dim=1, keepdim=True)
    assert allclose(row_sums, ones_like(row_sums))

    pooled_features = attn_weights @ features.flatten(start_dim=1)
    assert allclose(pooled_features, output_features)


@pytest.mark.parametrize("dim_in", [1, 3])
@pytest.mark.parametrize("dim_hid", [1, 4])
@pytest.mark.parametrize("dim_att", [1, 5])
@pytest.mark.parametrize("batch_size", [1, 7])
@pytest.mark.parametrize('attention_layer_cls', [AttentionLayer, GatedAttentionLayer])
def test_attentionlayer(dim_in: int, dim_hid: int, dim_att: int, batch_size: int,
                        attention_layer_cls: Type[Union[AttentionLayer, GatedAttentionLayer]]) -> None:
    attentionlayer = attention_layer_cls(
        input_dims=dim_in,
        hidden_dims=dim_hid,
        attention_dims=dim_att
    )
    _test_attention_layer(attentionlayer, dim_in, dim_att, batch_size)


@pytest.mark.parametrize("dim_in", [1, 3])
@pytest.mark.parametrize("batch_size", [1, 7])
def test_mean_pooling(dim_in: int, batch_size: int,) -> None:
    _test_attention_layer(MeanPoolingLayer(), dim_in=dim_in, dim_att=1, batch_size=batch_size)