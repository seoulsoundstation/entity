#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 블로그 전체 글 -> 마크다운 백업 (Playwright 최종판)

- 자바스크립트 렌더링까지 처리하여 인용/출처 블록(se_sectionArea)도 잡음
- 본문 이미지를 원본 화질로 다운로드해 옵시디언에 임베드
- 출처(인용)한 원본 글은 별도 .md 로 저장하고,
  원글에는 '출처: 제목(주소)' 한 줄만 남겨 길어지지 않게 함

사전 설치:
  pip install requests beautifulsoup4 markdownify playwright
  playwright install chromium
실행:
  python naver_blog_final.py
"""
import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as to_md
from playwright.sync_api import sync_playwright

# ================== 설정 (여기만 만지면 됩니다) ==================
BLOG_ID = "tmdejr1267"          # 백업할 블로그 ID
OUT_DIR = "naver_blog_backup"   # 결과 폴더
DELAY   = 0.5                   # 글/이미지 사이 대기(초)
DOWNLOAD_IMAGES = True          # 이미지 원본 화질 다운로드
FOLLOW_SOURCES  = True          # 출처(인용) 원본 글도 별도 저장
# ===============================================================

ATTACH_DIR = os.path.join(OUT_DIR, "attachments")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Referer": f"https://m.blog.naver.com/{BLOG_ID}",
}


def clean_json(text):
    return re.sub(r"^[)\]}',]*\s*", "", text.strip())


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:80] or "untitled"


def highres(url):
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    return re.sub(r"[?&]type=[^&]*", "", url)


def guess_ext(url, content_type=""):
    m = re.search(r"\.(jpe?g|png|gif|webp|bmp)(?:[?&]|$)", url, re.I)
    if m:
        return m.group(1).lower().replace("jpeg", "jpg")
    ct = content_type.lower()
    for e in ("png", "gif", "webp", "bmp"):
        if e in ct:
            return e
    return "jpg"


def parse_naver_link(href):
    if not href:
        return None
    if "naver.me/" in href:
        try:
            href = requests.head(href, headers=HEADERS,
                                 allow_redirects=True, timeout=10).url
        except Exception:
            return None
    m = re.search(r"blog\.naver\.com/PostView\.naver\?[^ ]*?blogId=([\w-]+)[^ ]*?logNo=(\d+)", href)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"(?:m\.)?blog\.naver\.com/([\w-]+)/(\d{8,})", href)
    if m:
        return m.group(1), m.group(2)
    return None



def existing_lognos(out_dir):
    """이미 저장된 .md 파일명에서 글 번호(logNo)를 수집 -> 중복 저장 방지."""
    done = set()
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):
            m = re.search(r"\((\d{8,})\)\.md$", fn)
            if m:
                done.add(m.group(1))
    return done


def get_log_nos(blog_id):
    log_nos, page = [], 1
    while True:
        url = f"https://m.blog.naver.com/api/blogs/{blog_id}/post-list"
        params = {"categoryNo": 0, "itemCount": 30, "page": page}
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            data = json.loads(clean_json(r.text))
        except Exception as e:
            print(f"  ! 목록 파싱 실패 (page {page}): {e}")
            break
        result = data.get("result", data)
        items = result.get("items") or result.get("postList") or []
        if not items:
            break
        for it in items:
            ln = it.get("logNo") or it.get("logNumber")
            if ln:
                log_nos.append(str(ln))
        total = result.get("totalCount")
        print(f"  목록 수집중... {len(log_nos)}개" + (f" / {total}" if total else ""))
        if total and len(log_nos) >= int(total):
            break
        page += 1
        time.sleep(DELAY)
    seen, uniq = set(), []
    for ln in log_nos:
        if ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return uniq


def download_image(src, log_no, idx, referer):
    headers = dict(HEADERS)
    headers["Referer"] = referer
    for cand in (highres(src), src):
        if not cand:
            continue
        try:
            resp = requests.get(cand, headers=headers, timeout=25)
            if resp.status_code == 200 and len(resp.content) > 1000:
                ext = guess_ext(cand, resp.headers.get("Content-Type", ""))
                fname = f"{log_no}_{idx:02d}.{ext}"
                with open(os.path.join(ATTACH_DIR, fname), "wb") as f:
                    f.write(resp.content)
                return fname
        except Exception:
            continue
    return None


def extract_post(soup, blog_id, log_no, do_images=True):
    """렌더링된 soup에서 (제목, 본문md, 출처줄들, 출처링크들, ok) 추출."""
    title_el = (soup.select_one("div.se-title-text")
                or soup.select_one(".se_title")
                or soup.select_one("h3.tit_h3")
                or soup.select_one("meta[property='og:title']")
                or soup.select_one("title"))
    if title_el is not None and title_el.name == "meta":
        title = (title_el.get("content") or "").strip() or log_no
    else:
        title = title_el.get_text(strip=True) if title_el else log_no

    body = (soup.select_one("div.se-main-container")
            or soup.select_one("#postViewArea")
            or soup.select_one("div.post_ct"))

    body_md = ""
    if body is not None:
        if do_images and DOWNLOAD_IMAGES:
            ref = f"https://m.blog.naver.com/{blog_id}/{log_no}"
            for idx, img in enumerate(body.find_all("img"), 1):
                src = img.get("data-lazy-src") or img.get("data-src") or img.get("src") or ""
                if not src:
                    continue
                fn = download_image(src, log_no, idx, ref)
                img.replace_with(f"\n![[{fn}]]\n" if fn else f"\n![]({highres(src)})\n")
                time.sleep(DELAY)
        body_md = to_md(str(body), heading_style="ATX", strip=["script", "style"])
        body_md = re.sub(r"\n{3,}", "\n\n", body_md).strip()

    # 출처(인용) 블록 — se_sectionArea 안의 첫 링크가 원본 글
    source_lines, source_links = [], []
    for sec in soup.select("div.se_sectionArea"):
        a = sec.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        txt = a.get_text(" ", strip=True) or "출처"
        link = parse_naver_link(href)
        if link:
            b, ln = link
            source_links.append((b, ln))
            source_lines.append(f"> 📌 출처: [[{ln}|{txt}]]  (원본: {href})")
        else:
            source_lines.append(f"> 📌 출처: [{txt}]({href})")

    ok = (body is not None) or bool(source_lines)
    return title, body_md, source_lines, source_links, ok


def save_md(blog_id, log_no, title, body_md, source_lines):
    fname = f"{safe_filename(title)} ({log_no}).md"
    with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f'aliases: ["{log_no}"]\n')
        f.write(f'본문주소: "https://m.blog.naver.com/{blog_id}/{log_no}"\n')
        f.write("---\n\n")
        f.write(f"# {title}\n\n")
        if body_md:
            f.write(body_md + "\n\n")
        if source_lines:
            f.write("\n".join(source_lines) + "\n")


def render(page, blog_id, log_no):
    url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
    page.goto(url, wait_until="networkidle", timeout=40000)
    try:
        page.wait_for_selector("div.se-main-container", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)
    return BeautifulSoup(page.content(), "html.parser")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if DOWNLOAD_IMAGES:
        os.makedirs(ATTACH_DIR, exist_ok=True)

    print(f"[1/3] '{BLOG_ID}' 글 목록 수집...")
    main_logs = get_log_nos(BLOG_ID)
    print(f"  -> 총 {len(main_logs)}개\n")
    if not main_logs:
        print("글을 못 찾았어요. BLOG_ID/공개여부를 확인해 주세요.")
        return

    done = existing_lognos(OUT_DIR)
    new_logs = [ln for ln in main_logs if ln not in done]
    print(f"  이미 저장됨 {len(done)}개 / 새 글 {len(new_logs)}개\n")
    if not new_logs:
        print("새로 추가할 글이 없어요. 종료합니다.")
        return

    saved = set((BLOG_ID, ln) for ln in done)
    all_sources = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("[2/3] 본문 저장 + 출처 수집...")
        for i, ln in enumerate(new_logs, 1):
            try:
                soup = render(page, BLOG_ID, ln)
                title, body_md, src_lines, src_links, ok = extract_post(soup, BLOG_ID, ln)
                save_md(BLOG_ID, ln, title, body_md, src_lines)
                saved.add((BLOG_ID, ln))
                all_sources += src_links
                tag = f"  (출처 {len(src_links)})" if src_links else ""
                print(f"  [{i}/{len(new_logs)}] {title[:30]}{tag}")
            except Exception as e:
                print(f"  [{i}/{len(main_logs)}] 실패({ln}): {e}")
            time.sleep(DELAY)

        if FOLLOW_SOURCES and all_sources:
            todo, seen = [], set()
            for b, ln in all_sources:
                if (b, ln) in saved or (b, ln) in seen or ln in done:
                    continue
                seen.add((b, ln))
                todo.append((b, ln))
            print(f"\n[3/3] 출처(인용) 원본 글 {len(todo)}개 별도 저장...")
            for i, (b, ln) in enumerate(todo, 1):
                try:
                    soup = render(page, b, ln)
                    title, body_md, src_lines, _, ok = extract_post(soup, b, ln)
                    save_md(b, ln, title, body_md, src_lines)
                    saved.add((b, ln))
                    print(f"  [{i}/{len(todo)}] {title[:30]}")
                except Exception as e:
                    print(f"  [{i}/{len(todo)}] 실패({b}/{ln}): {e}")
                time.sleep(DELAY)

        browser.close()

    print(f"\n완료! 저장 폴더: {OUT_DIR}  (이미지: {ATTACH_DIR})")


if __name__ == "__main__":
    main()
