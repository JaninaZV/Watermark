#!/usr/bin/env python3
"""
generate_text.py
================

Reference workload for the Watermark environmental footprint experiment.

What it does: loads a small instruction-tuned LLM and generates N text
completions from a fixed prompt set, saving each to disk. This represents
typical chat / assistant inference — the dominant LLM use case in production.

Why this model: meta-llama/Llama-3.2-3B-Instruct is small enough to run on a
single GPU in minutes while still being a realistic transformer inference
workload. If Llama is gated on your Hugging Face account, pass
--model Qwen/Qwen2.5-3B-Instruct (fully open, similar size).

Requirements:
    pip install torch transformers accelerate

Standalone run on a GPU instance:
    python3 generate_text.py --count 50 --output ./completions

Wrapped with the Watermark meter:
    watermark --region us-east-1 --output ./text_run -- \\
        python3 generate_text.py --count 50 --output ./completions

The prompt set is fixed and intentionally varied so the workload is
reproducible across runs.
"""

import argparse
import time
from pathlib import Path

# Fixed prompt set — questions, creative tasks, summarization-style requests.
PROMPTS = [
    "Explain why the sky appears blue during the day in two short paragraphs.",
    "Write a haiku about a data center humming at night.",
    "Summarize the key idea of the Paris Agreement on climate change in three bullet points.",
    "What are three practical ways a small team can reduce cloud compute costs?",
    "Describe a fictional city where every building generates its own solar power.",
    "List five interview questions for hiring an ML infrastructure engineer.",
    "Rewrite this sentence to be clearer: 'Due to the fact that latency was high, we rolled back.'",
    "Give a brief, neutral explanation of what a carbon intensity factor is.",
    "Write a polite email declining a meeting because of a schedule conflict.",
    "What is the difference between operational and embodied carbon in hardware?",
]

DEFAULT_MODEL = "meta-llama/Llama-3.2-3B-Instruct"
FALLBACK_MODEL = "Qwen/Qwen2.5-3B-Instruct"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate text completions with a small LLM — Watermark reference workload.",
    )
    parser.add_argument("--count", type=int, default=50,
                        help="Number of completions to generate (default: 50).")
    parser.add_argument("--output", default="./completions",
                        help="Output directory for generated text (default: ./completions).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}; "
                             f"try {FALLBACK_MODEL} if gated).")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="Maximum new tokens per completion (default: 256).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42).")
    return parser.parse_args(argv)


def main():
    args = parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate_text] Loading model: {args.model}")
    print(f"[generate_text] Fallback if gated: {FALLBACK_MODEL}")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.float16,
        device_map="auto",
    )
    model.eval()

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    print(f"[generate_text] Generating {args.count} completions "
          f"(max {args.max_tokens} new tokens each)...")
    started = time.time()
    completion_tokens = 0
    prompt_tokens = 0

    for i in range(args.count):
        prompt = PROMPTS[i % len(PROMPTS)]
        messages = [{"role": "user", "content": prompt}]
        if hasattr(tokenizer, "apply_chat_template"):
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
            )
        else:
            text = prompt

        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        prompt_len = int(inputs["input_ids"].shape[-1])
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=args.max_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )

        new_tokens = int(outputs.shape[-1]) - prompt_len
        completion_tokens += max(new_tokens, 0)
        prompt_tokens += prompt_len

        completion = tokenizer.decode(outputs[0], skip_special_tokens=True)
        (output_dir / f"completion_{i:04d}.txt").write_text(completion, encoding="utf-8")

        if (i + 1) % 10 == 0:
            elapsed = time.time() - started
            print(f"  {i + 1}/{args.count} completions   ({elapsed:.1f}s elapsed)")

    total = time.time() - started
    per_item = total / args.count
    print(f"[generate_text] Done. {args.count} completions in {total:.1f}s "
          f"({per_item:.2f}s each).")
    print(f"[generate_text] Tokens: {completion_tokens} completion "
          f"({prompt_tokens} prompt, {completion_tokens + prompt_tokens} total)")

    try:
        from workload_metrics import write_workload_metrics
        write_workload_metrics(output_dir, {
            "token_count": completion_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "total_tokens": completion_tokens + prompt_tokens,
            "request_count": args.count,
            "completion_count": args.count,
            "source": "generate_text.py",
        })
        print(f"[generate_text] Wrote {output_dir / 'workload_metrics.json'} for Watermark per-unit normalization")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
