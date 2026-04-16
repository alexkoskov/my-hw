#!/usr/bin/env python3
"""
Generate task files from tech-spec Implementation Tasks.
"""
import os
import re

FEATURE_PATH = os.path.dirname(__file__)
TASKS_DIR = os.path.join(FEATURE_PATH, 'tasks')
TEMPLATE_PATH = os.path.join(FEATURE_PATH, '../../molyanov-ai-dev-main/shared/work-templates/tasks/task.md.template')

# Task data extracted from tech-spec
tasks = [
    {
        'number': 1,
        'name': 'Configuration loader',
        'description': 'Create `feeds.json` configuration file format (JSON array of up to 5 strings). Write `load_feeds()` function that reads the file, validates URLs, and returns list. If file missing or invalid, fall back to hardcoded RSS URL.',
        'skills': ['code-writing'],
        'reviewers': ['code-reviewer', 'security-auditor', 'test-reviewer'],
        'verify': ['smoke'],
        'smoke_commands': [
            'python -c "import json; json.load(open(\'feeds.json\'))" → valid JSON',
            'python -c "from news_bot import load_feeds; print(load_feeds())" → list of URLs'
        ],
        'files_to_modify': ['news_bot.py', 'feeds.json'],
        'files_to_read': ['news_bot.py', 'patterns.md'],
        'depends_on': [],
        'wave': 1,
    },
    {
        'number': 2,
        'name': 'Feed iteration and error isolation',
        'description': 'Modify `job()` to iterate over feeds from `load_feeds()`. For each feed, wrap `fetch_rss` in try‑catch, log errors, collect entries. Aggregate entries before duplicate filtering. Keep global limit (`limit=3`) across all feeds.',
        'skills': ['code-writing'],
        'reviewers': ['code-reviewer', 'security-auditor', 'test-reviewer'],
        'verify': ['smoke'],
        'smoke_commands': ['Run script with test feeds (one invalid) and check logs for error isolation.'],
        'files_to_modify': ['news_bot.py'],
        'files_to_read': ['news_bot.py'],
        'depends_on': [],
        'wave': 1,
    },
    {
        'number': 3,
        'name': 'Enhanced logging',
        'description': 'Add feed source (URL) to log messages when processing entries. Include feed index in logs for clarity.',
        'skills': ['code-writing'],
        'reviewers': ['code-reviewer', 'test-reviewer'],
        'verify': ['smoke'],
        'smoke_commands': ['Run script with multiple feeds, verify logs contain feed identifiers.'],
        'files_to_modify': ['news_bot.py'],
        'files_to_read': ['patterns.md'],
        'depends_on': [],
        'wave': 1,
    },
    {
        'number': 4,
        'name': 'Unit tests for new functionality',
        'description': 'Write unit tests for `load_feeds`, feed iteration, error isolation. Use `unittest.mock` to simulate file I/O and network errors.',
        'skills': ['code-writing'],
        'reviewers': ['code-reviewer', 'test-reviewer'],
        'verify': ['smoke'],
        'smoke_commands': ['python -m pytest tests/ -xvs → all new tests pass.'],
        'files_to_modify': ['tests/test_news_bot.py'],
        'files_to_read': ['news_bot.py', 'patterns.md'],
        'depends_on': [1, 2, 3],
        'wave': 2,
    },
    {
        'number': 5,
        'name': 'Integration test with mock feeds',
        'description': 'Create integration test that runs the full pipeline with mock RSS feeds (local HTTP server serving static RSS XML) and mock Telegram API. Verify articles from multiple feeds are processed, duplicates skipped, errors isolated.',
        'skills': ['code-writing'],
        'reviewers': ['code-reviewer', 'test-reviewer'],
        'verify': ['smoke'],
        'smoke_commands': ['Run integration test suite; check that mock Telegram received expected posts.'],
        'files_to_modify': ['tests/integration/test_multiple_feeds.py'],
        'files_to_read': ['news_bot.py', 'patterns.md'],
        'depends_on': [1, 2, 3],
        'wave': 2,
    },
    {
        'number': 6,
        'name': 'Code Audit',
        'description': 'Full-feature code quality audit. Read all source files created/modified in this feature (`news_bot.py`, test files). Review holistically for cross‑component issues: duplicate resource initialization, architectural consistency, error‑handling completeness.',
        'skills': ['code-reviewing'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': ['news_bot.py', 'tests/test_news_bot.py', 'tests/integration/test_multiple_feeds.py'],
        'depends_on': [4, 5],
        'wave': 3,
    },
    {
        'number': 7,
        'name': 'Security Audit',
        'description': 'Full-feature security audit. Read all source files created/modified in this feature. Analyze for OWASP Top 10 across all components, cross‑component auth/data flow, input validation (URLs), secure file reading.',
        'skills': ['security-auditor'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': ['news_bot.py', 'tests/test_news_bot.py', 'tests/integration/test_multiple_feeds.py'],
        'depends_on': [4, 5],
        'wave': 3,
    },
    {
        'number': 8,
        'name': 'Test Audit',
        'description': 'Full-feature test quality audit. Read all test files created in this feature. Verify coverage, meaningful assertions, test pyramid balance across all components.',
        'skills': ['test-master'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': ['tests/test_news_bot.py', 'tests/integration/test_multiple_feeds.py'],
        'depends_on': [4, 5],
        'wave': 3,
    },
    {
        'number': 9,
        'name': 'Pre-deploy QA',
        'description': 'Acceptance testing: run all tests (unit, integration), verify acceptance criteria from user‑spec and tech‑spec, ensure no regression on single‑feed mode.',
        'skills': ['pre-deploy-qa'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': [],
        'depends_on': [6, 7, 8],
        'wave': 4,
    },
    {
        'number': 10,
        'name': 'Deploy (optional)',
        'description': 'Deploy updated bot to production server (if applicable). Update configuration file on server, restart service.',
        'skills': ['deploy-pipeline'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': [],
        'depends_on': [9],
        'wave': 5,
    },
    {
        'number': 11,
        'name': 'Post-deploy verification (optional)',
        'description': 'Live environment verification: run bot with production feeds, check Telegram channel for new posts, verify logs for any errors.',
        'skills': ['post-deploy-qa'],
        'reviewers': [],
        'verify': [],
        'smoke_commands': [],
        'files_to_modify': [],
        'files_to_read': [],
        'depends_on': [10],
        'wave': 5,
    },
]

def read_template():
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()

def generate_task_file(task):
    template = read_template()
    # Replace frontmatter
    frontmatter = f"""---
status: planned
depends_on: {task['depends_on']}
wave: {task['wave']}
skills: {task['skills']}
verify: {task['verify']}
reviewers: {task['reviewers']}
teammate_name:
---"""
    # Find the frontmatter block in template (lines between --- and ---)
    # We'll replace the whole frontmatter section
    lines = template.split('\n')
    # Find first ---
    try:
        start = lines.index('---')
        end = lines.index('---', start + 1)
        # Replace lines[start:end+1] with frontmatter lines
        new_lines = lines[:start] + frontmatter.split('\n') + lines[end+1:]
    except ValueError:
        # fallback
        new_lines = lines
    content = '\n'.join(new_lines)
    # Replace title
    title = f"# Task {task['number']}: {task['name']}"
    content = re.sub(r'# Task N: Название', title, content)
    # Replace Required Skills
    skills_text = '\n'.join([f'- `/skill:{skill}` — [skills/{skill}/SKILL.md](~/.claude/skills/{skill}/SKILL.md)' for skill in task['skills']])
    # Find the Required Skills section (between '## Required Skills' and next '##')
    # For simplicity, we'll replace the whole section after the heading
    # We'll use a placeholder? The template has placeholder lines after heading.
    # Let's replace from "Перед выполнением задачи загрузи:" line to next blank line?
    # Instead, we'll rebuild the entire section.
    # We'll replace from "## Required Skills" up to before next "##"
    # Use regex
    pattern = r'(## Required Skills\n\n)(.*?)(\n## )'
    replacement = f'## Required Skills\n\nПеред выполнением задачи загрузи:\n{skills_text}\n\n\\3'
    content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    # Replace Description
    desc_pattern = r'(## Description\n\n)(.*?)(\n## )'
    desc_replacement = f'## Description\n\n{task["description"]}\n\n\\3'
    content = re.sub(desc_pattern, desc_replacement, content, flags=re.DOTALL)
    # Replace What to do (placeholder)
    what_pattern = r'(## What to do\n\n)(.*?)(\n## )'
    what_replacement = f'## What to do\n\nКонкретные шаги — ЧТО, не КАК. Не псевдокод.\n\n\\3'
    content = re.sub(what_pattern, what_replacement, content, flags=re.DOTALL)
    # TDD Anchor: delete for non-code tasks? According to skill, delete for non-code tasks.
    # We'll keep placeholder for code-writing tasks, else delete section.
    if task['skills'] == ['code-writing']:
        # Keep placeholder
        pass
    else:
        # Delete TDD Anchor section
        tdd_pattern = r'(## TDD Anchor\n\n)(.*?)(\n## )'
        content = re.sub(tdd_pattern, '', content, flags=re.DOTALL)
    # Acceptance Criteria placeholder
    acc_pattern = r'(## Acceptance Criteria\n\n)(.*?)(\n## )'
    acc_replacement = f'## Acceptance Criteria\n\n- [ ] Критерий 1\n- [ ] Критерий 2\n\n\\3'
    content = re.sub(acc_pattern, acc_replacement, content, flags=re.DOTALL)
    # Context Files: add extra files
    # We'll keep default list and add files_to_modify and files_to_read
    # For simplicity, just keep default.
    # Verification Steps: add smoke if present
    smoke_section = ''
    if task['verify'] and 'smoke' in task['verify']:
        smoke_lines = '\n'.join([f'- {cmd}' for cmd in task['smoke_commands']])
        smoke_section = f'### Smoke\n\n{smoke_lines}\n\n'
    # Replace Verification Steps section
    verif_pattern = r'(## Verification Steps\n\n)(.*?)(\n## )'
    verif_replacement = f'## Verification Steps\n\n### Automated\n\n- `pytest tests/test_xxx.py -v` → all pass\n\n{smoke_section}\\3'
    content = re.sub(verif_pattern, verif_replacement, content, flags=re.DOTALL)
    # Details section placeholder
    # Keep as is
    # Reviewers section: replace with actual reviewers
    reviewers_section = ''
    for reviewer in task['reviewers']:
        reviewers_section += f'- **{reviewer}** → `work/multiple-rss-feeds/logs/working/task-{task["number"]}/{reviewer}-{{round}}.json`\n'
    if reviewers_section:
        rev_pattern = r'(## Reviewers\n\n)(.*?)(\n## )'
        content = re.sub(rev_pattern, f'## Reviewers\n\n{reviewers_section}\\3', content, flags=re.DOTALL)
    # Write file
    os.makedirs(TASKS_DIR, exist_ok=True)
    filepath = os.path.join(TASKS_DIR, f'{task["number"]}.md')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'Generated {filepath}')

def main():
    for task in tasks:
        generate_task_file(task)

if __name__ == '__main__':
    main()