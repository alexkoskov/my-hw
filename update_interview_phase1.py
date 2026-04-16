#!/usr/bin/env python3
import sys
import datetime

file_path = sys.argv[1] if len(sys.argv) > 1 else 'work/multiple-rss-feeds/logs/userspec/interview.yml'

with open(file_path, 'r') as f:
    lines = f.readlines()

# Helper to find line index of a pattern after a certain line
def find_item(lines, start_idx, item_name):
    for i in range(start_idx, len(lines)):
        if lines[i].strip().startswith(item_name + ':'):
            return i
    return -1

# Update feature_description value and score
for i, line in enumerate(lines):
    if line.strip().startswith('feature_description:'):
        # find value line within next 10 lines
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Улучшение существующего бота: добавление поддержки нескольких RSS-лент для агрегации новостей из нескольких источников Hot Wheels."\n'
                break
        # find score line
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 70\n'
                break
        # update gaps
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: "Достаточно подробно описано, но можно уточнить детали реализации."\n'
                break
        break

# Update user_problem
for i, line in enumerate(lines):
    if line.strip().startswith('user_problem:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Одна RSS-лента ограничивает охват новостей. Нужно агрегировать новости из нескольких источников Hot Wheels для более полного покрытия."\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 85\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: ""\n'
                break
        break

# Update success_criteria
for i, line in enumerate(lines):
    if line.strip().startswith('success_criteria:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Бот должен обрабатывать все добавленные RSS-ленты, не пропускать новости, постить каждую новость в Telegram-канал с переводом и картинкой."\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 85\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: ""\n'
                break
        break

# Update constraints
for i, line in enumerate(lines):
    if line.strip().startswith('constraints:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Не больше 5 RSS-лент (ограничение по производительности), обновление раз в день, ограничения Telegram (длина сообщений, размер изображений)."\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 85\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: ""\n'
                break
        break

# Update testing_strategy
for i, line in enumerate(lines):
    if line.strip().startswith('testing_strategy:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Интеграционные тесты с mock RSS-лентами для проверки парсинга и обработки. E2E тесты не требуются, так как основная логика уже покрыта существующим ботом."\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 85\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: ""\n'
                break
        break

# Update target_users (optional, fill based on project knowledge)
for i, line in enumerate(lines):
    if line.strip().startswith('target_users:'):
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('value:'):
                lines[j] = '    value: "Подписчики Telegram-канала Hot Wheels News (энтузиасты и коллекционеры), которые хотят получать новости из нескольких источников."\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('score:'):
                lines[j] = '    score: 70\n'
                break
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith('gaps:'):
                lines[j] = '    gaps: "Предполагается на основе проекта."\n'
                break
        break

# Update last_updated timestamp
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
for i, line in enumerate(lines):
    if line.strip().startswith('last_updated:'):
        lines[i] = f'  last_updated: "{now}"\n'
        break

# Write back
with open(file_path, 'w') as f:
    f.writelines(lines)

print(f'Updated {file_path} with phase1 scores')