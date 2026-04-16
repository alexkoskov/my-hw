#!/usr/bin/env python3
import sys
import datetime

file_path = sys.argv[1] if len(sys.argv) > 1 else 'my-hw/work/multiple-rss-feeds/logs/userspec/interview.yml'

with open(file_path, 'r') as f:
    lines = f.readlines()

# Current timestamp
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# Mapping of patterns to replacements
replacements = {
    'started: ""': f'started: "{now}"',
    'status: "not_started"': 'status: "in_progress"',
}

# Phase1 feature_name value
# Find line with 'feature_name:' then look ahead for 'value: ""'
i = 0
while i < len(lines):
    if lines[i].strip().startswith('feature_name:'):
        # Look ahead up to 10 lines for 'value: ""'
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value: ""'):
                lines[j] = '    value: "multiple-rss-feeds"\n'
                break
    i += 1

# Phase1 work_type value
i = 0
while i < len(lines):
    if lines[i].strip().startswith('work_type:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value: ""'):
                lines[j] = '    value: "feature"\n'
                break
    i += 1

# Apply simple replacements
for idx, line in enumerate(lines):
    for old, new in replacements.items():
        if old in line:
            lines[idx] = line.replace(old, new)
            break

# Write back
with open(file_path, 'w') as f:
    f.writelines(lines)

print(f'Updated {file_path}')