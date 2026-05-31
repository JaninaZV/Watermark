#!/usr/bin/env python3
"""
generate_embeddings.py
======================

Reference workload for the Watermark environmental footprint experiment.

What it does: loads a small sentence-transformer model and encodes N text
snippets into embedding vectors, saving them as a single NumPy array.
This represents RAG indexing, semantic search, and retrieval pipelines —
increasingly common background AI workloads.

Why this model: sentence-transformers/all-MiniLM-L6-v2 is small (~80MB),
fast on CPU or GPU, and widely used as a baseline embedding model. It
produces realistic batch encoding without the cost of a full LLM run.

Requirements:
    pip install sentence-transformers numpy

Standalone run:
    python3 generate_embeddings.py --count 500 --output ./embeddings_out

Wrapped with the Watermark meter:
    watermark --region us-east-1 --output ./embed_run -- \\
        python3 generate_embeddings.py --count 500 --output ./embeddings_out

Uses a fixed set of 100 short sentences, looped to reach --count.
"""

import argparse
import time
from pathlib import Path

# Fixed sentence set — varied topics, looped to reach --count.
SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Machine learning models require careful validation on held-out data.",
    "Solar panels convert sunlight directly into electricity.",
    "The Pacific Ocean is the largest ocean on Earth.",
    "Version control helps teams collaborate on software safely.",
    "Photosynthesis converts carbon dioxide and water into glucose.",
    "A balanced diet includes vegetables, protein, and whole grains.",
    "Neural networks learn representations from training examples.",
    "The speed of light in a vacuum is approximately 299,792 km per second.",
    "Open-source software can be inspected and improved by anyone.",
    "Rainforests produce a significant share of the world's oxygen.",
    "Encryption protects data confidentiality during transmission.",
    "The human heart pumps blood through the circulatory system.",
    "Cloud data centers consume both electricity and water for cooling.",
    "Regular exercise supports cardiovascular and mental health.",
    "Compilers translate high-level code into machine instructions.",
    "Antibiotics treat bacterial infections but not viral ones.",
    "Satellite imagery helps monitor deforestation and urban growth.",
    "A database index speeds up read queries at some write cost.",
    "Bees play a critical role in pollinating many food crops.",
    "The Renaissance began in Italy during the fourteenth century.",
    "HTTP is the foundation of data communication on the web.",
    "Glaciers store most of the world's fresh water as ice.",
    "Microservices split applications into independently deployable parts.",
    "Shakespeare wrote plays that are still performed worldwide.",
    "Wind turbines generate electricity without burning fuel.",
    "Caching reduces latency by storing frequently accessed data.",
    "The mitochondria are often called the powerhouse of the cell.",
    "Public transit can reduce urban traffic and emissions.",
    "Object-oriented programming groups data with behavior.",
    "Volcanoes form where tectonic plates converge or diverge.",
    "Load balancers distribute traffic across multiple servers.",
    "The Amazon River carries more water than any other river.",
    "Unit tests verify that individual components behave correctly.",
    "Desert ecosystems adapt to extreme heat and scarce rainfall.",
    "APIs define how software components communicate with each other.",
    "The moon's gravity causes ocean tides on Earth.",
    "Continuous integration automates building and testing on every change.",
    "Fungi decompose organic matter and recycle nutrients in soil.",
    "Latency-sensitive applications benefit from edge computing.",
    "The printing press accelerated the spread of knowledge in Europe.",
    "Container orchestration manages deployment of many services.",
    "Coral reefs support extraordinary marine biodiversity.",
    "Strong passwords and multi-factor auth improve account security.",
    "The greenhouse effect traps heat in Earth's atmosphere.",
    "Batch inference amortizes model loading over many inputs.",
    "Migratory birds navigate using magnetic fields and landmarks.",
    "Graph databases store relationships between entities explicitly.",
    "Hydropower dams generate electricity from flowing water.",
    "Technical documentation helps onboard new engineers faster.",
    "Permafrost thaw releases methane stored in Arctic soils.",
    "Feature flags allow gradual rollout of new functionality.",
    "The periodic table organizes elements by atomic number.",
    "Retrieval-augmented generation combines search with language models.",
    "Earthquakes occur when stress is released along fault lines.",
    "Observability combines metrics, logs, and traces for debugging.",
    "Wetlands filter pollutants and reduce flood risk.",
    "Floating-point arithmetic can introduce small rounding errors.",
    "Carbon accounting tracks emissions from energy and materials.",
    "The Roman Empire once spanned much of Europe and the Mediterranean.",
    "Prompt engineering shapes model outputs without retraining.",
    "Algae blooms can deplete oxygen and harm aquatic life.",
    "Immutable infrastructure reduces configuration drift in production.",
    "The circulatory system delivers oxygen to tissues.",
    "Embedding models map text into dense vector spaces.",
    "Thunderstorms form when warm moist air rises rapidly.",
    "Rate limiting protects services from overload and abuse.",
    "Biodiversity loss weakens ecosystem resilience.",
    "Static typing catches errors before code runs.",
    "The water cycle moves water between oceans, air, and land.",
    "Fine-tuning adapts a pretrained model to a specific task.",
    "Urban heat islands raise temperatures in dense cities.",
    "Message queues decouple producers and consumers.",
    "Photos taken from space reveal large-scale environmental change.",
    "Knowledge distillation compresses large models into smaller ones.",
    "Mangrove forests protect coastlines from storm surges.",
    "Infrastructure as code defines servers and networks in files.",
    "The brain uses neurons to transmit electrical signals.",
    "Semantic search finds documents by meaning, not just keywords.",
    "Droughts stress agriculture and municipal water supplies.",
    "Garbage collection reclaims memory in managed runtimes.",
    "Transformer attention relates every token to every other token.",
    "Recycling reduces demand for virgin raw materials.",
    "Blue-green deployment switches traffic between two environments.",
    "Plate tectonics explains the movement of continents.",
    "Vector databases store embeddings for similarity search.",
    "Evaporative cooling in data centers consumes freshwater.",
    "Pair programming can improve code quality and knowledge sharing.",
    "The ozone layer absorbs most harmful ultraviolet radiation.",
    "Model quantization reduces memory and speeds up inference.",
    "Estuaries mix fresh and salt water where rivers meet the sea.",
    "Secrets managers store credentials outside application code.",
    "Inference serving scales models behind a stable API endpoint.",
    "Soil erosion reduces agricultural productivity over time.",
    "Event-driven architectures react to messages as they arrive.",
    "Life cycle assessment evaluates impacts from cradle to grave.",
    "Coastal erosion reshapes shorelines over decades.",
    "Hyperparameter tuning searches for better model settings.",
    "Renewable energy capacity has grown rapidly in the last decade.",
    "Distributed tracing follows requests across microservices.",
    "Embodied carbon includes manufacturing impacts of hardware.",
    "Groundwater aquifers recharge slowly in arid regions.",
    "Water usage effectiveness measures cooling water per IT kilowatt-hour.",
]

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate text embeddings — Watermark reference workload.",
    )
    parser.add_argument("--count", type=int, default=500,
                        help="Number of embeddings to generate (default: 500).")
    parser.add_argument("--output", default="./embeddings_out",
                        help="Output directory for embeddings.npy (default: ./embeddings_out).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Sentence-transformers model ID (default: {DEFAULT_MODEL}).")
    return parser.parse_args(argv)


def main():
    args = parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate_embeddings] Loading model: {args.model}")
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model)

    texts = [SENTENCES[i % len(SENTENCES)] for i in range(args.count)]
    print(f"[generate_embeddings] Encoding {args.count} sentences "
          f"(from {len(SENTENCES)}-sentence fixed set)...")
    started = time.time()

    batch_size = 32
    chunks = []
    for start in range(0, args.count, batch_size):
        batch = texts[start:start + batch_size]
        vectors = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
        chunks.append(vectors)
        done = min(start + batch_size, args.count)
        if done % 100 == 0 or done == args.count:
            elapsed = time.time() - started
            print(f"  {done}/{args.count} embeddings   ({elapsed:.1f}s elapsed)")

    embeddings = np.vstack(chunks)
    out_path = output_dir / "embeddings.npy"
    np.save(out_path, embeddings)

    total = time.time() - started
    per_item = total / args.count
    print(f"[generate_embeddings] Done. Saved {embeddings.shape} array to {out_path} "
          f"in {total:.1f}s ({per_item:.3f}s each).")


if __name__ == "__main__":
    main()
