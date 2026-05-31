#!/usr/bin/env python3
"""
generate_images.py
==================

Reference workload for the Watermark environmental footprint experiment.

What it does: loads Stable Diffusion XL on a GPU and generates N images from
a fixed set of prompts, saving each to disk. This is the workload we wrap
with watermark_meter.py to produce a measured per-image energy / carbon /
water footprint.

Why this workload: image generation is the most legible "AI uses resources"
story in the current public conversation (Luccioni et al., FAccT 2024) and
it has the advantage of being technically simple — no training, no dataset
prep, no fine-tuning complexity. About 5-8 minutes on a single A100.

Requirements:
    pip install diffusers transformers accelerate torch safetensors
    (these are pre-installed on most GPU rental templates labeled
    "Hugging Face", "PyTorch", or "Diffusers")

Standalone run on a GPU instance:
    python3 generate_images.py --count 100 --output ./images

Wrapped with the Watermark meter (the real experiment):
    python3 watermark_meter.py --region us-east-1 --output ./va_run -- \\
        python3 generate_images.py --count 100 --output ./images

The prompt set is fixed and intentionally varied so the workload is
reproducible — anyone running the same script on the same model with the
same seed and step count gets the same compute, which means anyone can
verify your numbers.
"""

import argparse
import time
from pathlib import Path

# A small, fixed prompt set. Variety keeps the model's working state realistic;
# fixing the set keeps the workload reproducible across runs.
PROMPTS = [
    "a serene mountain lake at sunrise, cinematic lighting",
    "a busy market street in tokyo at night, neon signs",
    "a cat wearing a small wizard hat, oil painting style",
    "an astronaut floating above earth, photorealistic",
    "a vintage bicycle leaning against a brick wall in spring",
    "a chef plating a colorful dish, overhead view, studio lighting",
    "an old library with tall wooden shelves and a stained glass window",
    "a hummingbird in flight near a tropical flower, macro photography",
    "a small wooden cabin in a snowy forest at golden hour",
    "a desert highway stretching to the horizon, light motion blur",
]


def main():
    parser = argparse.ArgumentParser(
        description="Generate images with Stable Diffusion XL — Watermark reference workload.",
    )
    parser.add_argument("--count", type=int, default=100,
                        help="Number of images to generate (default: 100).")
    parser.add_argument("--output", default="./images",
                        help="Output directory for generated images (default: ./images).")
    parser.add_argument("--model", default="stabilityai/stable-diffusion-xl-base-1.0",
                        help="Hugging Face model ID (default: SDXL base 1.0).")
    parser.add_argument("--steps", type=int, default=30,
                        help="Denoising steps per image (default: 30 — standard quality).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42).")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate_images] Loading model: {args.model}")
    print("[generate_images] (first run downloads ~7GB; subsequent runs use the cache)")
    import torch
    from diffusers import StableDiffusionXLPipeline

    pipe = StableDiffusionXLPipeline.from_pretrained(
        args.model,
        torch_dtype=torch.float16,
        use_safetensors=True,
    ).to("cuda")

    generator = torch.Generator(device="cuda").manual_seed(args.seed)

    print(f"[generate_images] Generating {args.count} images at {args.steps} steps each...")
    started = time.time()

    for i in range(args.count):
        prompt = PROMPTS[i % len(PROMPTS)]
        image = pipe(
            prompt,
            num_inference_steps=args.steps,
            generator=generator,
        ).images[0]
        image.save(output_dir / f"img_{i:04d}.png")
        if (i + 1) % 10 == 0:
            elapsed = time.time() - started
            print(f"  {i + 1}/{args.count} images   ({elapsed:.1f}s elapsed)")

    total = time.time() - started
    per_image = total / args.count
    print(f"[generate_images] Done. {args.count} images in {total:.1f}s "
          f"({per_image:.2f}s per image).")


if __name__ == "__main__":
    main()
