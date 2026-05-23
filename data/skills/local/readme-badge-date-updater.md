---
name: readme-badge-date-updater
description: Update the "Last Update" badge in README.md to the current date.
category: docs
---
This skill uses a patch operation to replace the static message in the Last Update badge used in README.md. It expects the README to contain an image badge of the form:

[![Last Update](https://img.shields.io/static/v1?label=last%20update&message=*OLD*&color=blue)](...)

The skill replaces *OLD* with the date or message provided.

### Usage
```
# Update README badge to today
skill: readme-badge-date-updater
# environment variable READ_DATE can be set to '2026-04-24' or any custom string.
```

The patch is performed by comparing the exact old string and replacing it with the new one.
