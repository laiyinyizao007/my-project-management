#!/usr/bin/env python3
"""Duplicate Issue detection for GitHub Issues.

Usage:
    python dedup.py <issue_number> <issue_title> <repo> <gh_token>
"""

import json
import os
import re
import sys
import urllib.request
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.6
MAX_SIMILAR_TO_REPORT = 5
MIN_MEANINGFUL_TOKENS = 2

PREFIX_RE = re.compile(
    r'^(feat|feature|fix|docs?|chore|refactor|test|tests|ci|cd|perf|build|style|revert)'
    r'(\([^)]*\))?!?:\s*',
    re.IGNORECASE,
)

STOP_WORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'be', 'has', 'have',
    'fix', 'fixes', 'fixed', 'add', 'adds', 'added', 'update', 'updates',
    'support', 'implement', 'when', 'that', 'this', 'it', 'if',
    'feat', 'feature', 'docs', 'doc', 'chore', 'test', 'tests',
    'refactor', 'ci', 'cd', 'perf', 'style',
}


def normalize_title(title: str) -> str:
    return PREFIX_RE.sub('', title.strip()).strip()


def tokenize(text: str) -> set:
    words = re.findall(r'[a-z0-9]+', text.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 1}


def sequence_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def jaccard_similarity(a: str, b: str) -> float:
    words_a = tokenize(a)
    words_b = tokenize(b)
    union = words_a | words_b
    if not union:
        return 0.0
    return len(words_a & words_b) / len(union)


def combined_score(a: str, b: str) -> float:
    a = normalize_title(a)
    b = normalize_title(b)
    return max(sequence_similarity(a, b), jaccard_similarity(a, b))


def get_open_issues(repo: str, exclude_number: int, token: str) -> list:
    issues = []
    page = 1
    while True:
        url = (
            f'https://api.github.com/repos/{repo}/issues'
            f'?state=open&per_page=100&page={page}'
        )
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('X-GitHub-Api-Version', '2022-11-28')
        with urllib.request.urlopen(req) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        for issue in batch:
            # Skip pull requests (they appear in issues list too)
            if 'pull_request' in issue:
                continue
            if issue['number'] == exclude_number:
                continue
            issues.append(issue)
        if len(batch) < 100:
            break
        page += 1
    return issues


def post_comment(repo: str, issue_number: int, body: str, token: str) -> None:
    url = f'https://api.github.com/repos/{repo}/issues/{issue_number}/comments'
    data = json.dumps({'body': body}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    with urllib.request.urlopen(req) as resp:
        print(f'Posted comment (HTTP {resp.status})')


def main() -> None:
    if len(sys.argv) != 5:
        print(f'Usage: {sys.argv[0]} <issue_number> <issue_title> <repo> <gh_token>')
        sys.exit(1)

    issue_number = int(sys.argv[1])
    issue_title = sys.argv[2]
    repo = sys.argv[3]
    token = sys.argv[4]

    normalized = normalize_title(issue_title)
    meaningful = tokenize(normalized)
    if len(meaningful) < MIN_MEANINGFUL_TOKENS:
        print(
            f'Only {len(meaningful)} meaningful word(s) in "{normalized}". '
            'Too short to compare — skipping.'
        )
        return

    print(f'Checking for duplicates of: "{issue_title}" (normalized: "{normalized}")')
    existing_issues = get_open_issues(repo, issue_number, token)
    print(f'Comparing against {len(existing_issues)} open issue(s)...')

    scored = []
    for issue in existing_issues:
        score = combined_score(issue_title, issue['title'])
        if score >= SIMILARITY_THRESHOLD:
            scored.append((score, issue))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MAX_SIMILAR_TO_REPORT]

    if not top:
        print('No similar issues found.')
        return

    lines = [
        '## ⚠️ 可能存在重复 Issue',
        '',
        f'以下 Issue 与本 Issue 标题高度相似（相似度阈值：{SIMILARITY_THRESHOLD:.0%}）：',
        '',
    ]
    for score, issue in top:
        lines.append(
            f'- #{issue["number"]} [{issue["title"]}]({issue["html_url"]}) '
            f'— 相似度 {score:.0%}'
        )
    lines += [
        '',
        '> 如果这不是重复 Issue，请忽略此提示。',
    ]
    body = '\n'.join(lines)

    print(f'Found {len(top)} similar issue(s). Posting comment...')
    post_comment(repo, issue_number, body, token)


if __name__ == '__main__':
    main()
