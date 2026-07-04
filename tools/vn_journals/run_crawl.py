from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.models import JournalSeed
from tools.vn_journals.parsers.ojs_crawler import OjsCrawler

DEFAULT_SEED_PATH = REPO_ROOT / "data/vietnam_journals/vn_journal_seed.json"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data/vietnam_journals/crawl_preview.json"


def load_seeds(path: Path) -> list[JournalSeed]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [JournalSeed(**row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview/crawl Vietnamese university journals.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    crawlers = [OjsCrawler()]
    results = []

    for seed in load_seeds(args.seed):
        crawler = next((candidate for candidate in crawlers if candidate.can_handle(seed)), None)
        if crawler is None:
            results.append({"journal": asdict(seed), "articles": [], "warnings": [f"No crawler for platform={seed.platform}"]})
            continue
        results.append(asdict(crawler.crawl(seed, limit=args.limit)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote preview to {args.output}")


if __name__ == "__main__":
    main()
