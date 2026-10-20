"""Create a small, untrained DFlash2 GGUF for adapter correctness checks.

Requires the pinned backend's gguf-py and numpy. This fixture measures no model
quality or speed; a real target still verifies every proposed token.
"""
import argparse
from pathlib import Path
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backends/llamacpp/gguf-py'))
import gguf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reader = gguf.GGUFReader(args.target)
    arch = reader.fields['general.architecture'].contents()
    hidden = int(reader.fields[f'{arch}.embedding_length'].contents())
    layers = int(reader.fields[f'{arch}.block_count'].contents())
    vocab = len(reader.fields['tokenizer.ggml.tokens'].contents())
    assert hidden >= 272 and hidden % 128 == 0 and layers >= 4
    writer = gguf.GGUFWriter(args.output, 'dflash')
    for key, field in reader.fields.items():
        if key.startswith('tokenizer.'):
            writer.add_key_value(key, field.contents(), field.types[0], field.types[1] if len(field.types) > 1 else None)
    writer.add_string('general.name', 'Synthetic untrained DFlash2 adapter fixture')
    integers = {'block_count': 1, 'context_length': 8192, 'embedding_length': hidden,
        'feed_forward_length': hidden * 2, 'attention.head_count': hidden // 128,
        'attention.head_count_kv': 2, 'attention.key_length': 128, 'attention.value_length': 128,
        'rope.dimension_count': 128, 'block_size': 8, 'conv_kernel_size': 2,
        'conv_group_size': 16, 'selector_rank': 8, 'selector_top_k': 16,
        'attention.sliding_window': 64}
    for key, value in integers.items(): writer.add_uint32('dflash.' + key, value)
    writer.add_float32('dflash.attention.layer_norm_rms_epsilon', 1e-6)
    writer.add_float32('dflash.rope.freq_base', 10000000.0)
    writer.add_bool('dflash.attention.causal', False)
    taps = [0, layers // 3, layers - 2]
    writer.add_array('dflash.target_layers', taps)
    writer.add_array('dflash.rope.dimension_sections', [64, 0, 0, 0])
    writer.add_array('dflash.attention.sliding_window_pattern', [True])
    if 'tokenizer.ggml.mask_token_id' not in reader.fields:
        writer.add_uint32('tokenizer.ggml.mask_token_id', vocab - 1)
    rng = np.random.default_rng(7319)
    def tensor(name, shape, norm=False):
        value = np.ones(shape, dtype=np.float32) if norm else (rng.standard_normal(shape) * .01).astype(np.float16)
        writer.add_tensor(name, value)
    def named(kind): return gguf.TENSOR_NAMES[kind] + '.weight'
    tensor(named(gguf.MODEL_TENSOR.FC), (hidden, len(taps) * hidden))
    tensor(named(gguf.MODEL_TENSOR.ENC_OUTPUT_NORM), (hidden,), True)
    tensor('output_norm.weight', (hidden,), True)
    tensor(named(gguf.MODEL_TENSOR.DFLASH_SELECTOR_PREV), (vocab, 8))
    tensor(named(gguf.MODEL_TENSOR.DFLASH_SELECTOR_NEXT), (vocab, 8))
    tensor(named(gguf.MODEL_TENSOR.DFLASH_SELECTOR_HIDDEN), (8, hidden))
    for suffix, shape, norm in [
        ('attn_norm.weight', (hidden,), True), ('attn_q.weight', (hidden, hidden), False),
        ('attn_k.weight', (256, hidden), False), ('attn_v.weight', (256, hidden), False),
        ('attn_output.weight', (hidden, hidden), False),
        ('attn_q_norm.weight', (128,), True), ('attn_k_norm.weight', (128,), True),
        ('ffn_norm.weight', (hidden,), True), ('ffn_gate.weight', (hidden * 2, hidden), False),
        ('ffn_up.weight', (hidden * 2, hidden), False), ('ffn_down.weight', (hidden, hidden * 2), False)]:
        tensor('blk.0.' + suffix, shape, norm)
    for kind in ('attn', 'ffn'):
        base = np.zeros((2, 2, hidden), dtype=np.float32); base[:, 0, :] = 1
        writer.add_tensor(f'blk.0.{kind}_conv_base', base)
        writer.add_tensor(f'blk.0.{kind}_conv_proj.weight', np.zeros((4 * (hidden // 16), hidden), dtype=np.float16))
    writer.write_header_to_file(); writer.write_kv_data_to_file(); writer.write_tensors_to_file(); writer.close()
    print(args.output, args.output.stat().st_size)


if __name__ == '__main__': main()
