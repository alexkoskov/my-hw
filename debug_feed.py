#!/usr/bin/env python3
import feedparser
feed = feedparser.parse('https://www.autoevolution.com/rss/tag-Hot+Wheels.xml')
print(f'Entries: {len(feed.entries)}')
if feed.entries:
    e = feed.entries[0]
    print('Keys:', list(e.keys()))
    for key in ['summary', 'description', 'content', 'title', 'link', 'published']:
        if key in e:
            print(f'{key}: {e[key][:100]}')
    # Check if description contains HTML
    if 'description' in e:
        import re
        desc = e.description
        # strip HTML tags
        text = re.sub(r'<[^>]+>', '', desc)
        print(f'Description stripped: {text[:200]}')