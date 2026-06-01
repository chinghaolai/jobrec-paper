import json
import re
import time
import yaml
import logging
import argparse
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

logging.basicConfig(
    format='[%(asctime)s %(levelname)s] %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.INFO
)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ABS_URL = "https://arxiv.org/abs/"
_NS = {
    "atom":  "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _text(el, tag: str) -> str:
    return (el.findtext(tag, namespaces=_NS) or "").strip()


def get_authors(author_els, first_author: bool = False) -> str:
    names = [_text(a, "atom:name") for a in author_els]
    return names[0] if first_author else ", ".join(names)


def sort_papers_by_id(papers: dict) -> dict:
    return dict(sorted(papers.items(), reverse=True))


def load_config(config_file: Path) -> dict:
    if not config_file.exists():
        logging.error(f"Configuration file not found: {config_file}")
        raise SystemExit(1)

    with open(config_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    keyword_queries = {}
    for key, value in config.get("keywords", {}).items():
        parts = []
        for term in value["filters"]:
            # Search in title field; quote multi-word terms
            parts.append(f'ti:"{term}"' if " " in term else f"ti:{term}")
        keyword_queries[key] = " OR ".join(parts)

    config["keyword_queries"] = keyword_queries
    logging.info(f"Config loaded — topics: {list(keyword_queries.keys())}")
    return config


def _fetch_batch(query: str, start: int, batch: int, max_retries: int = 5) -> list:
    """Calls the arXiv API and returns a list of <entry> Element objects.
    Retries with exponential backoff on 429 / transient errors."""
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": start,
        "max_results": batch,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    url = f"{ARXIV_API_URL}?{params}"
    logging.info(f"Fetching: {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "daily-arxiv-fetcher/1.0"})
    delay = 10
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            return root.findall("atom:entry", _NS)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                logging.warning(f"Rate limited (429). Waiting {delay}s before retry {attempt}/{max_retries}...")
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except Exception:
            if attempt < max_retries:
                logging.warning(f"Request failed. Waiting {delay}s before retry {attempt}/{max_retries}...")
                time.sleep(delay)
                delay *= 2
            else:
                raise
    return []


def get_daily_papers(topic: str, query: str, max_results: int) -> dict:
    """Fetches papers from the arXiv HTTP API (no external library required)."""
    papers = {}
    start = 0
    batch_size = min(max_results, 500)  # arXiv recommends ≤ 500 per call

    while start < max_results:
        try:
            entries = _fetch_batch(query, start, batch_size)
        except Exception as exc:
            logging.error(f"arXiv API error at start={start}: {exc}")
            break

        if not entries:
            break

        for entry in entries:
            title = re.sub(r"\s+", " ", _text(entry, "atom:title"))

            if "job recommendation" not in title.lower():
                continue

            entry_id = _text(entry, "atom:id")
            m = re.search(r"abs/([\d.]+)", entry_id)
            if not m:
                continue
            paper_key = m.group(1)

            published = _text(entry, "atom:published")
            pub_date = published[:10] if published else "N/A"

            summary = re.sub(r"\s+", " ", _text(entry, "atom:summary"))
            author_els = entry.findall("atom:author", _NS)

            primary_cat_el = entry.find("arxiv:primary_category", _NS)
            primary_category = primary_cat_el.get("term", "") if primary_cat_el is not None else ""

            logging.info(f"  + [{pub_date}] {title[:80]}")

            papers[paper_key] = {
                "title": title,
                "authors": get_authors(author_els),
                "first_author": get_authors(author_els, first_author=True),
                "url": f"{ARXIV_ABS_URL}{paper_key}",
                "pdf_url": f"{ARXIV_ABS_URL}{paper_key}",
                "publish_date": pub_date,
                "abstract": summary,
                "primary_category": primary_category,
                "code_url": "null",
            }

        fetched = len(entries)
        start += fetched
        if fetched < batch_size:
            break

        time.sleep(3)  # arXiv asks for polite crawling

    logging.info(f"Topic '{topic}': {len(papers)} papers found.")
    return {topic: papers}


def update_json_file(filename: Path, new_data: dict):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            content = f.read()
            json_data = json.loads(content) if content else {}
    except FileNotFoundError:
        json_data = {}

    for topic, papers in new_data.items():
        json_data.setdefault(topic, {}).update(papers)

    filename.parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=4, ensure_ascii=False)


def json_to_md(json_file: Path, md_file: Path, **kwargs):
    to_web    = kwargs.get("to_web", False)
    use_title = kwargs.get("use_title", True)
    use_tc    = kwargs.get("use_tc", True)
    use_b2t   = kwargs.get("use_b2t", True)

    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    date_now = datetime.date.today().strftime("%Y-%m-%d")

    md_file.parent.mkdir(parents=True, exist_ok=True)
    with open(md_file, "w", encoding="utf-8") as f:
        if to_web and use_title:
            f.write("---\nlayout: default\n---\n\n")

        if use_title:
            f.write(f"## Updated on {date_now}\n")
        else:
            f.write(f"> Updated on {date_now}\n")

        if use_tc and data:
            f.write("<details>\n <summary>Table of Contents</summary>\n <ol>\n")
            for topic in data:
                if data[topic]:
                    anchor = topic.replace(" ", "-").lower()
                    f.write(f'    <li><a href="#{anchor}">{topic}</a></li>\n')
            f.write(" </ol>\n</details>\n\n")

        for topic, papers in data.items():
            if not papers:
                continue

            f.write(f"## {topic}\n\n")
            f.write("| Publish Date | Title | Authors | PDF | Code |\n")
            f.write("|:---|:---|:---|:---|:---|\n")

            for paper_id, details in sort_papers_by_id(papers).items():
                if not isinstance(details, dict):
                    logging.warning(f"Skipping old-format entry: {paper_id}")
                    continue

                pub_date = details.get("publish_date", "N/A")
                title    = details.get("title", "N/A").replace("|", "\\|")
                authors  = details.get("authors", "N/A")
                pdf_url  = details.get("pdf_url", "#")
                code_url = details.get("code_url", "null")

                code_link = f"[{paper_id}]({code_url})" if code_url != "null" else "N/A"
                f.write(f"| {pub_date} | **{title}** | {authors} | [Link]({pdf_url}) | {code_link} |\n")

            f.write("\n")
            if use_b2t:
                f.write('<p align=right>(<a href="#">back to top</a>)</p>\n\n')

    logging.info(f"Markdown written: {md_file}")


def process_publication_target(target_name: str, config: dict, data_collector: list):
    json_path = Path(config[f"json_{target_name}_path"])
    md_path   = Path(config[f"md_{target_name}_path"])

    for data in data_collector:
        update_json_file(json_path, data)

    json_to_md(json_path, md_path, **{
        "to_web":    target_name == "gitpage",
        "use_title": target_name != "wechat",
        "use_tc":    target_name == "readme",
        "show_badge": config.get("show_badge", True),
        "use_b2t":   target_name != "gitpage",
    })
    logging.info(f"Target '{target_name}' done.")


def main(**config):
    data_collector = []

    logging.info("=== Starting daily arXiv fetch ===")
    for topic, query in config.get("keyword_queries", {}).items():
        logging.info(f"Topic: '{topic}' | Query: {query}")
        result = get_daily_papers(
            topic=topic,
            query=query,
            max_results=config.get("max_results", 200),
        )
        if result.get(topic):
            data_collector.append(result)
    logging.info("=== Fetch complete ===")

    targets = {
        "readme":   config.get("publish_readme"),
        "gitpage":  config.get("publish_gitpage"),
        "wechat":   config.get("publish_wechat"),
    }
    for target, enabled in targets.items():
        if enabled:
            process_publication_target(target, config, data_collector)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily arXiv paper tracker (no external dependencies).")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()
    config = load_config(args.config)
    main(**config)
