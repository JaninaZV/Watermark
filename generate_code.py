#!/usr/bin/env python3
"""
generate_code.py
================

Reference workload for the Watermark environmental footprint experiment.

What it does: loads a small code model and generates N code completions from
a fixed prompt set, saving each to disk. This represents IDE copilot /
code-assistant inference — a fast-growing slice of AI compute.

Why this model: bigcode/starcoder2-3b is deliberately small (3B parameters)
so a full reference run finishes in minutes on one GPU while exercising
real code-token generation. Qwen/Qwen2.5-Coder-1.5B is an even smaller
fallback via --model.

Requirements:
    pip install torch transformers accelerate

Standalone run on a GPU instance:
    python3 generate_code.py --count 50 --output ./code_out

Wrapped with the Watermark meter:
    watermark --region us-east-1 --output ./code_run -- \\
        python3 generate_code.py --count 50 --output ./code_out

The prompt set is fixed and intentionally varied so the workload is
reproducible across runs.
"""

import argparse
import time
from pathlib import Path

# Fixed code-completion prompt set — function bodies, bug fixes, refactors.
PROMPTS = [
    "def merge_sorted_lists(a, b):\n    \"\"\"Merge two sorted lists into one sorted list.\"\"\"\n",
    "def is_palindrome(s: str) -> bool:\n    \"\"\"Return True if s is a palindrome, ignoring case and spaces.\"\"\"\n",
    "# Fix: this function should return the sum, not the product\n"
    "def add_numbers(nums):\n    total = 1\n    for n in nums:\n        total *= n\n    return total\n\n# Fixed version:\n",
    "def fetch_json(url: str, timeout: int = 10) -> dict:\n    \"\"\"GET url and parse JSON response.\"\"\"\n",
    "class RingBuffer:\n    def __init__(self, capacity: int):\n        self.capacity = capacity\n        self.buffer = []\n\n    def push(self, item):\n",
    "# Refactor: extract validation into a helper\n"
    "def create_user(email, age):\n    if '@' not in email:\n        raise ValueError('invalid email')\n    if age < 0:\n        raise ValueError('invalid age')\n    return {'email': email, 'age': age}\n",
    "def binary_search(arr, target):\n    \"\"\"Return index of target in sorted arr, or -1.\"\"\"\n",
    "async def read_lines(path: Path):\n    \"\"\"Async generator yielding non-empty stripped lines.\"\"\"\n",
    "def parse_csv_row(row: str) -> dict:\n    \"\"\"Parse a CSV row with quoted fields into a dict.\"\"\"\n",
    "def memoize(fn):\n    \"\"\"Decorator caching results by positional arguments.\"\"\"\n    cache = {}\n    def wrapper(*args):\n",
]

DEFAULT_MODEL = "bigcode/starcoder2-3b"
FALLBACK_MODEL = "Qwen/Qwen2.5-Coder-1.5B"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate code completions with a small code LLM — Watermark reference workload.",
    )
    parser.add_argument("--count", type=int, default=50,
                        help="Number of completions to generate (default: 50).")
    parser.add_argument("--output", default="./code_out",
                        help="Output directory for generated code (default: ./code_out).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}; "
                             f"try {FALLBACK_MODEL} for smaller/faster).")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="Maximum new tokens per completion (default: 256).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42).")
    return parser.parse_args(argv)


def main():
    args = parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate_code] Loading model: {args.model}")
    print(f"[generate_code] Smaller fallback: {FALLBACK_MODEL}")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.eval()

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    print(f"[generate_code] Generating {args.count} code completions "
          f"(max {args.max_tokens} new tokens each)...")
    started = time.time()

    for i in range(args.count):
        prompt = PROMPTS[i % len(PROMPTS)]
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=args.max_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )

        completion = tokenizer.decode(outputs[0], skip_special_tokens=True)
        (output_dir / f"completion_{i:04d}.py").write_text(completion, encoding="utf-8")

        if (i + 1) % 10 == 0:
            elapsed = time.time() - started
            print(f"  {i + 1}/{args.count} completions   ({elapsed:.1f}s elapsed)")

    total = time.time() - started
    per_item = total / args.count
    print(f"[generate_code] Done. {args.count} completions in {total:.1f}s "
          f"({per_item:.2f}s each).")


if __name__ == "__main__":
    main()
