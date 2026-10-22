import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

import requests

SITEMAPS = [
    "https://ankergames.net/sitemap_post_2.xml",
    "https://ankergames.net/sitemap_post_1.xml",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY_BETWEEN_GAMES = 1.5   # polite delay between page reads
DELAY_AFTER_ERROR = 5.0     # backoff delay on HTTP failure
CSRF_REFRESH_INTERVAL = 50  # refresh CSRF token periodically
DELAY_AFTER_DL_URL = 13.0   # minimum spacing between API calls to avoid 429
OUTPUT_FILE = Path("ankergames.json")
PROGRESS_FILE = Path("progress.json")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def refresh_csrf(session: requests.Session) -> str:
    resp = session.get("https://ankergames.net/csrf-token", timeout=15)
    resp.raise_for_status()
    return resp.json()["token"]


def get_game_urls_from_sitemaps(session: requests.Session, newest: int = 0) -> list[str]:
    entries = []  # (lastmod, url)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for sitemap_url in SITEMAPS:
        resp = session.get(sitemap_url, timeout=15)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        for url_el in root.findall("sm:url", ns):
            loc = url_el.find("sm:loc", ns).text
            if "/game/" not in loc:
                continue
            lm = url_el.find("sm:lastmod", ns)
            entries.append((lm.text if lm is not None else "", loc))
    print(f"[sitemaps] Found {len(entries)} game URLs")

    if newest:
        if any(lm for lm, _ in entries):
            entries.sort(key=lambda e: e[0], reverse=True)
            print(f"[sitemaps] Keeping {newest} most recently modified")
        else:
            print("[sitemaps] No <lastmod> found in sitemap; using trailing entries as newest")
            entries.reverse()
        entries = entries[:newest]
    return [u for _, u in entries]


def parse_game_page(html: str) -> dict | None:
    """Extract metadata and download ID from the game page."""
    jsonld_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    )
    if not jsonld_match:
        return None

    try:
        data = json.loads(jsonld_match.group(1))
    except json.JSONDecodeError:
        return None

    items = data if isinstance(data, list) else [data]
    game_data = next((i for i in items if i.get("@type") == "VideoGame"), None)
    if not game_data:
        return None

    dl_id_match = re.search(r"generateDownloadUrl\((\d+)\)", html)
    download_id = int(dl_id_match.group(1)) if dl_id_match else None

    raw_date = game_data.get("dateModified") or game_data.get("datePublished", "")
    if raw_date and "T" not in raw_date:
        raw_date = raw_date + "T00:00:00.000Z"

    return {
        "title": game_data.get("name", "").strip(),
        "fileSize": game_data.get("fileSize", "").strip(),
        "uploadDate": raw_date,
        "downloadId": download_id,
    }


def generate_download_url(
    session: requests.Session, csrf: str, download_id: int, referer: str,
    max_retries: int = 6
) -> str | None:
    """Request signed intermediate link and handle rate limiting."""
    for _ in range(max_retries):
        resp = session.post(
            f"https://ankergames.net/generate-download-url/{download_id}",
            json={"g-recaptcha-response": "development-mode"},
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer,
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 419:
            return None  # CSRF expired
        if resp.status_code == 429:
            try:
                data = resp.json()
                wait_sec = int(data.get("retry_after", 35)) + 3
            except Exception:
                wait_sec = 38
            print(f" [429 wait {wait_sec}s]", end="", flush=True)
            time.sleep(wait_sec)
            continue
        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        if data.get("success") and data.get("download_url"):
            return data["download_url"]

        error_msg = data.get("error", "")
        rl_match = re.search(r"wait\s+(\d+)\s+second", error_msg, re.IGNORECASE)
        if rl_match:
            wait_sec = int(rl_match.group(1)) + 2
            print(f" [rl {wait_sec}s]", end="", flush=True)
            time.sleep(wait_sec)
            continue
        return None
    return None


def extract_dlproxy_url(session: requests.Session, ankergames_dl_url: str, referer: str = "") -> str | None:
    """Inspects redirects and page body to resolve datanodes or dlproxy targets."""
    headers = {}
    if referer:
        headers["Referer"] = referer

    try:
        resp = session.get(ankergames_dl_url, headers=headers, timeout=15, allow_redirects=True)
    except Exception:
        return None

    # Check if requests was directly redirected to the destination host
    final_url = resp.url
    if "datanodes.to" in final_url or "dlproxy.uk" in final_url:
        if "dlproxy.uk" in final_url:
            return re.sub(r"tunnel\d+\.dlproxy\.uk", "tunnel1.dlproxy.uk", final_url)
        return final_url

    # Check response history for location headers
    for history_resp in resp.history:
        loc = history_resp.headers.get("Location", "")
        if "datanodes.to" in loc or "dlproxy.uk" in loc:
            if "dlproxy.uk" in loc:
                return re.sub(r"tunnel\d+\.dlproxy\.uk", "tunnel1.dlproxy.uk", loc)
            return loc

    # Parse body and normalize unescaped slashes
    text = unquote(resp.text.replace(r"\/", "/"))

    # Check for direct downloadPage function calls
    matches = re.findall(r"downloadPage\(['\"]([^'\"]+)['\"]", text)
    if matches:
        raw_url = matches[0]
        if "dlproxy.uk" in raw_url:
            return re.sub(r"tunnel\d+\.dlproxy\.uk", "tunnel1.dlproxy.uk", raw_url)
        return raw_url

    # Check for direct host links anywhere in script tags
    raw_matches = re.findall(r"https?://[^\s'\"<>]*(?:datanodes\.to|dlproxy\.uk)[^\s'\"<>]*", text)
    if raw_matches:
        raw_url = raw_matches[0]
        if "dlproxy.uk" in raw_url:
            return re.sub(r"tunnel\d+\.dlproxy\.uk", "tunnel1.dlproxy.uk", raw_url)
        return raw_url

    return None


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "failed": []}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False)


def save_output(downloads: list) -> None:
    downloads = sorted(downloads, key=lambda d: d.get("uploadDate", ""), reverse=True)
    catalog = {
        "name": "AnkerGames",
        "downloads": downloads,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"\n[output] Saved {len(downloads)} games to {OUTPUT_FILE}")


def scrape(args: argparse.Namespace) -> None:
    session = make_session()
    progress = load_progress() if args.resume else {"completed": {}, "failed": []}

    print("[init] Refreshing CSRF token...")
    csrf = refresh_csrf(session)
    print("[init] CSRF ready")

    print("[sitemaps] Fetching game list...")
    all_urls = get_game_urls_from_sitemaps(session, args.newest)

    if args.limit:
        all_urls = all_urls[: args.limit]
        print(f"[limit] Processing first {args.limit} games")

    downloads = list(progress["completed"].values())
    pending = [u for u in all_urls if u not in progress["completed"]]
    print(f"[progress] {len(progress['completed'])} done, {len(pending)} remaining\n")

    for idx, game_url in enumerate(pending, start=1):
        slug = game_url.rstrip("/").split("/")[-1]

        if idx % CSRF_REFRESH_INTERVAL == 0:
            try:
                csrf = refresh_csrf(session)
                print(f"  [csrf] Refreshed at game {idx}")
            except Exception:
                pass

        print(f"[{idx}/{len(pending)}] {slug}", end="", flush=True)

        try:
            resp = session.get(game_url, timeout=20)
            if resp.status_code != 200:
                print(f" -> HTTP {resp.status_code} SKIP")
                progress["failed"].append(game_url)
                save_progress(progress)
                time.sleep(DELAY_AFTER_ERROR)
                continue

            meta = parse_game_page(resp.text)
            if not meta or not meta.get("title"):
                print(" -> parse error SKIP")
                progress["failed"].append(game_url)
                save_progress(progress)
                continue

            entry = {
                "title": meta["title"],
                "uris": [game_url],
                "uploadDate": meta["uploadDate"],
                "fileSize": meta["fileSize"],
            }

            if not args.no_dl and meta.get("downloadId"):
                dl_signed = generate_download_url(
                    session, csrf, meta["downloadId"], game_url
                )
                if dl_signed:
                    dlproxy = extract_dlproxy_url(session, dl_signed, referer=game_url)
                    if dlproxy:
                        entry["uris"] = [dlproxy]
                        print(" -> got URL", end="")
                    else:
                        print(" -> no proxy URL", end="")
                else:
                    try:
                        csrf = refresh_csrf(session)
                        dl_signed = generate_download_url(
                            session, csrf, meta["downloadId"], game_url
                        )
                        if dl_signed:
                            dlproxy = extract_dlproxy_url(session, dl_signed, referer=game_url)
                            if dlproxy:
                                entry["uris"] = [dlproxy]
                                print(" -> retried OK", end="")
                    except Exception:
                        pass

            print(f" -- {meta['fileSize']}")

            downloads.append(entry)
            progress["completed"][game_url] = entry
            save_progress(progress)

            if idx % 50 == 0:
                save_output(downloads)

        except KeyboardInterrupt:
            print("\n[interrupt] Saving progress...")
            save_output(downloads)
            save_progress(progress)
            sys.exit(0)
        except Exception as e:
            print(f" -> ERROR: {e}")
            progress["failed"].append(game_url)
            save_progress(progress)
            time.sleep(DELAY_AFTER_ERROR)
            continue

        time.sleep(DELAY_BETWEEN_GAMES)

    save_output(downloads)
    if progress["failed"]:
        print(f"\n[warn] {len(progress['failed'])} games failed.")


def fix_urls(args: argparse.Namespace) -> None:
    """Processes progress entries still holding intermediate ankergames.net links."""
    progress = load_progress()
    if not progress["completed"]:
        print("[fix] No progress file found. Run the scraper first.")
        return

    session = make_session()
    csrf = refresh_csrf(session)
    print("[fix] CSRF ready. Scanning for games missing direct download URLs...")

    # Identify any items whose current URI contains ankergames.net
    needs_fix = {
        url: entry
        for url, entry in progress["completed"].items()
        if not entry.get("uris")
        or any("ankergames.net" in u for u in entry.get("uris", []))
    }
    print(f"[fix] {len(needs_fix)} games need target host resolution\n")

    fixed = 0
    for idx, (game_url, entry) in enumerate(needs_fix.items(), start=1):
        slug = game_url.rstrip("/").split("/")[-1]
        print(f"[{idx}/{len(needs_fix)}] {slug}", end="", flush=True)

        if idx % CSRF_REFRESH_INTERVAL == 0:
            try:
                csrf = refresh_csrf(session)
                print("  [csrf] Refreshed")
            except Exception:
                pass

        try:
            current_uri = entry["uris"][0] if entry.get("uris") else ""

            # Attempt extraction directly if intermediate download-file link is already stored
            dlproxy = None
            if "download-file" in current_uri:
                dlproxy = extract_dlproxy_url(session, current_uri, referer=game_url)

            # If that failed, regenerate via the API
            if not dlproxy:
                resp = session.get(game_url, timeout=20)
                meta = parse_game_page(resp.text)
                if not meta or not meta.get("downloadId"):
                    print(" -> no download ID SKIP")
                    time.sleep(DELAY_BETWEEN_GAMES)
                    continue

                dl_signed = generate_download_url(session, csrf, meta["downloadId"], game_url)
                if not dl_signed:
                    csrf = refresh_csrf(session)
                    dl_signed = generate_download_url(session, csrf, meta["downloadId"], game_url)

                if dl_signed:
                    dlproxy = extract_dlproxy_url(session, dl_signed, referer=game_url)

            if dlproxy:
                entry["uris"] = [dlproxy]
                progress["completed"][game_url] = entry
                save_progress(progress)
                print(" -> fixed")
                fixed += 1
                time.sleep(DELAY_AFTER_DL_URL)
            else:
                print(" -> no proxy URL")
                time.sleep(DELAY_BETWEEN_GAMES)

        except KeyboardInterrupt:
            print("\n[interrupt] Saving...")
            save_progress(progress)
            break
        except Exception as e:
            print(f" -> ERROR: {e}")
            time.sleep(DELAY_BETWEEN_GAMES)

    print(f"\n[fix] Fixed {fixed}/{len(needs_fix)} games")
    downloads = list(progress["completed"].values())
    save_output(downloads)


def main() -> None:
    parser = argparse.ArgumentParser(description="AnkerGames -> Hydra JSON scraper")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N games")
    parser.add_argument("--newest", type=int, default=0, help="Process the N most recently updated games")
    parser.add_argument("--resume", action="store_true", help="Resume interrupted scrape")
    parser.add_argument("--no-dl", action="store_true", help="Skip download URL generation")
    parser.add_argument("--fix-urls", action="store_true", help="Second pass: fix unresolved download URLs")
    args = parser.parse_args()

    if args.fix_urls:
        fix_urls(args)
    else:
        scrape(args)


if __name__ == "__main__":
    main()
