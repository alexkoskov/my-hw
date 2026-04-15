# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Python script that automatically collects Hot Wheels news from autoevolution.com, translates them to Russian, summarizes, and posts to a Telegram channel.

This bot runs on a schedule (daily) and handles the entire pipeline from RSS fetching to Telegram posting, eliminating manual work for news aggregation and translation.

---

## Target Audience

**Primary users:** Hot Wheels enthusiasts and collectors who want to stay updated with the latest news in Russian.

**Use case:** Users subscribe to a Telegram channel where they receive automated, translated summaries of Hot Wheels news, saving time and overcoming language barriers.

---

## Core Problem

Manually monitoring Hot Wheels news across websites is time‑consuming, and many enthusiasts are not comfortable reading English content. This results in missed updates and delayed information.

Currently users have to regularly check multiple sites, translate articles themselves, and manually share them. This is slow and inconsistent because it relies on manual effort. We solve this by automating the entire process: RSS monitoring, article scraping, translation, summarization, and Telegram posting.

---

## Key Features

- **RSS monitoring** – Fetches the latest articles from the Hot Wheels RSS feed.
- **Duplicate detection** – Uses SQLite to track already processed news and avoid reposting.
- **Article scraping** – Extracts title, full text, and images from each article.
- **Translation** – Translates title and text from English to Russian using Google Translate.
- **Summarization** – Creates a short summary (3–5 sentences) of the translated text.
- **Telegram posting** – Sends formatted posts with images to a Telegram channel via Bot API.
- **Scheduling** – Runs daily at 12:00 local time (configurable) using the `schedule` library.

---

## Out of Scope

- No mobile app version.
- No web dashboard or admin panel.
- No multi‑language support beyond Russian.
- No real‑time notifications outside scheduled runs.
- No user authentication or personalization.
