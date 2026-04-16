#!/usr/bin/env python3
"""
Simple validation of task files against template.
Generates a JSON report similar to task-validator.
"""
import os
import re
import json
import ast
from datetime import datetime

FEATURE_PATH = os.path.dirname(__file__)
TASKS_DIR = os.path.join(FEATURE_PATH, 'tasks')
LOG_DIR = os.path.join(FEATURE_PATH, 'logs', 'tasks')
os.makedirs(LOG_DIR, exist_ok=True)

def parse_frontmatter(content):
    """Extract YAML frontmatter between --- lines."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return None
    front = match.group(1)
    # simple parsing: each line key: value
    data = {}
    for line in front.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        key = key.strip()
        val = val.strip()
        # try to parse YAML-like values
        if val.startswith('[') and val.endswith(']'):
            try:
                val = ast.literal_eval(val)
            except (SyntaxError, ValueError):
                pass
        elif val.lower() == 'true':
            val = True
        elif val.lower() == 'false':
            val = False
        elif val.isdigit():
            val = int(val)
        elif val.replace('.', '', 1).isdigit() and val.count('.') == 1:
            val = float(val)
        data[key] = val
    return data

def extract_sections(content):
    """Extract markdown sections after frontmatter."""
    # Remove frontmatter
    content_no_front = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL)
    # Split by ## headings
    sections = {}
    current_heading = None
    current_lines = []
    for line in content_no_front.split('\n'):
        if line.startswith('## '):
            if current_heading is not None:
                sections[current_heading] = '\n'.join(current_lines).strip()
            current_heading = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading is not None:
        sections[current_heading] = '\n'.join(current_lines).strip()
    return sections

def validate_frontmatter(data, task_num):
    findings = []
    required = ['status', 'depends_on', 'wave', 'skills', 'reviewers', 'verify']
    for field in required:
        if field not in data:
            findings.append({
                'severity': 'major',
                'issue': f'Missing frontmatter field: {field}',
                'fix': f'Add {field}: appropriate value'
            })
    # status should be planned
    if data.get('status') != 'planned':
        findings.append({
            'severity': 'minor',
            'issue': f'status should be "planned", got {data.get("status")}',
            'fix': 'Set status: planned'
        })
    # depends_on should be list
    depends = data.get('depends_on')
    if depends is not None and not isinstance(depends, list):
        findings.append({
            'severity': 'minor',
            'issue': f'depends_on should be a list, got {depends}',
            'fix': 'Set depends_on: []'
        })
    # wave number >= 1
    wave = data.get('wave')
    if wave is not None and (not isinstance(wave, int) or wave < 1):
        findings.append({
            'severity': 'minor',
            'issue': f'wave should be integer >=1, got {wave}',
            'fix': 'Set wave: correct wave number'
        })
    # skills should be list
    skills = data.get('skills')
    if skills is not None and not isinstance(skills, list):
        findings.append({
            'severity': 'minor',
            'issue': f'skills should be a list, got {skills}',
            'fix': 'Set skills: [skill1, skill2]'
        })
    # reviewers list
    reviewers = data.get('reviewers')
    if reviewers is not None and not isinstance(reviewers, list):
        findings.append({
            'severity': 'minor',
            'issue': f'reviewers should be a list, got {reviewers}',
            'fix': 'Set reviewers: [reviewer1, reviewer2]'
        })
    # verify list
    verify = data.get('verify')
    if verify is not None and not isinstance(verify, list):
        findings.append({
            'severity': 'minor',
            'issue': f'verify should be a list, got {verify}',
            'fix': 'Set verify: [] or [smoke] or [user]'
        })
    return findings

def validate_sections(sections, task_num, data):
    findings = []
    expected_sections = [
        'Required Skills',
        'Description',
        'What to do',
        'TDD Anchor',
        'Acceptance Criteria',
        'Context Files',
        'Verification Steps',
        'Details',
        'Reviewers',
        'Post-completion'
    ]
    # Check presence
    for sec in expected_sections:
        if sec not in sections:
            findings.append({
                'severity': 'major',
                'issue': f'Missing section: {sec}',
                'fix': f'Add ## {sec} section with appropriate content'
            })
        else:
            content = sections[sec]
            if not content or content.strip() == '':
                findings.append({
                    'severity': 'minor',
                    'issue': f'Section {sec} is empty',
                    'fix': f'Fill ## {sec} with appropriate content'
                })
            # Check placeholder text
            placeholders = [
                'Конкретные шаги — ЧТО, не КАК',
                'Критерий 1',
                'Критерий 2',
                'Тесты, которые нужно написать ДО реализации',
                'путь/к/файлу.ts — что сделать',
                'pytest tests/test_xxx.py -v'
            ]
            for ph in placeholders:
                if ph in content:
                    findings.append({
                        'severity': 'minor',
                        'issue': f'Section {sec} contains placeholder text',
                        'fix': f'Replace placeholder with specific content'
                    })
    # TDD Anchor conditional
    skills = data.get('skills', [])
    if 'code-writing' not in skills:
        # non-code task should not have TDD Anchor
        if 'TDD Anchor' in sections:
            findings.append({
                'severity': 'minor',
                'issue': 'Non-code task has TDD Anchor section',
                'fix': 'Delete TDD Anchor section'
            })
    else:
        # code task should have TDD Anchor with actual tests
        if 'TDD Anchor' in sections:
            content = sections['TDD Anchor']
            if 'tests/test_api.py::test_create_user' in content:
                findings.append({
                    'severity': 'minor',
                    'issue': 'TDD Anchor contains example placeholder',
                    'fix': 'Replace with actual test descriptions'
                })
    return findings

def validate_task(task_num):
    path = os.path.join(TASKS_DIR, f'{task_num}.md')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    frontmatter = parse_frontmatter(content)
    if frontmatter is None:
        return [{
            'severity': 'major',
            'issue': 'No frontmatter found',
            'fix': 'Ensure frontmatter delimited by ---'
        }]
    sections = extract_sections(content)
    findings = []
    findings.extend(validate_frontmatter(frontmatter, task_num))
    findings.extend(validate_sections(sections, task_num, frontmatter))
    return findings

def main():
    batch_number = 1
    iteration = 1
    all_findings = []
    for task_num in range(1, 12):
        findings = validate_task(task_num)
        for f in findings:
            f['task'] = task_num
        all_findings.extend(findings)
    # Write report
    report = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'batch': batch_number,
        'iteration': iteration,
        'findings': all_findings
    }
    report_path = os.path.join(LOG_DIR, f'template-batch{batch_number}-review.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f'Validation report written to {report_path}')
    print(f'Total findings: {len(all_findings)}')
    # Print summary
    for finding in all_findings:
        print(f"Task {finding['task']}: {finding['severity']} - {finding['issue']}")

if __name__ == '__main__':
    main()