# Grokipedia Citation Verifier

An enhanced frontend for [Grokipedia](https://grokipedia.com) that uses **Grok's web search** to verify Wikipedia-style citations in real-time. Built for the xAI Hackathon.

## What It Does

Grokipedia generates AI-written encyclopedia articles with citations, but how do you know if those citations actually support the claims? This app solves that by:

1. **Displaying Grokipedia articles** with a clean, dark-themed UI
2. **Verifying each citation** using Grok-4's web search to fetch the actual source
3. **Showing relevant quotes** from the source that support (or don't support) the claim
4. **Explaining the connection** between the claim and the evidence

### Demo

Click any citation `[33]` in an article to see:
- **Context**: The actual quote from the source
- **Insight**: How the source supports the specific claim
- **Feedback**: Rate if the citation is helpful

## Features

### Interactive Citation Cards
- **Hover preview**: Quick peek at source domain
- **Click to expand**: Full citation analysis with Context/Insight tabs
- **Real-time loading**: Citations fetch and cache on-demand
- **Thumbs up/down**: User feedback on citation quality

### AI-Powered Verification
- Uses **Grok-4** with web search tool to read actual sources
- Extracts **specific quotes** that verify the claim
- Generates **one-sentence explanations** of how the source supports the claim
- Fetches **actual page titles** from sources (not relying on potentially wrong metadata)

### Performance Optimizations
- **Two-tier caching**: In-memory + Supabase for instant repeat visits
- **Streaming responses**: Citations load progressively via SSE
- **Parallel processing**: Multiple citations analyzed simultaneously
- **Batch queries**: Efficient database lookups

### Data Fixes
- **Citation URL correction**: Grokipedia's JSON has mismatched URLs; we extract correct URLs from the embedded references list
- **KaTeX cleanup**: Currency values like `$200` were being corrupted by LaTeX rendering; we fix this automatically

## Tech Stack

- **Backend**: Python/Flask
- **AI**: xAI SDK with Grok-4 + web search
- **Database**: Supabase (PostgreSQL)
- **Frontend**: Vanilla JS with CSS animations

## Setup

### 1. Clone and create virtual environment

```bash
git clone <repo>
cd wiki-verification
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r grokipedia/requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```bash
XAI_API_KEY=your-xai-api-key
SUPABASE_URL=your-supabase-url
SUPABASE_KEY=your-supabase-anon-key
```

### 4. Set up Supabase tables

Run these SQL commands in your Supabase SQL editor:

```sql
-- Article cache (stores fetched Grokipedia pages)
CREATE TABLE article_cache (
    id SERIAL PRIMARY KEY,
    page_name TEXT UNIQUE NOT NULL,
    markdown TEXT,
    rendered_html TEXT,
    citations JSONB,
    fetched_at TIMESTAMP DEFAULT NOW()
);

-- Citation cache (stores AI analysis results)
CREATE TABLE citation_cache (
    id SERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    claim_context TEXT NOT NULL,
    relevant_quote TEXT,
    summary TEXT,
    title TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_url, claim_context)
);

-- User feedback on citations
CREATE TABLE citation_feedback (
    id SERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    citation_id TEXT,
    page_path TEXT,
    vote TEXT,
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_url, page_path)
);
```

### 5. Run the app

```bash
cd grokipedia
python app.py
```

Visit `http://localhost:5001`

## Usage

1. **Search or browse**: Enter a topic or paste a Grokipedia URL
2. **Read the article**: Browse the Wikipedia-style content
3. **Click any citation**: e.g., `[33]` to see verification
4. **Toggle Context/Insight**: See the quote or explanation
5. **Give feedback**: Thumbs up/down if the citation is helpful

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/page/<name>` | GET | View a Grokipedia article |
| `/api/citation/<id>` | GET | Get citation analysis (cached or fresh) |
| `/api/citations/batch` | POST | Check cache for multiple citations |
| `/api/citations/stream` | POST | Stream citation analysis via SSE |
| `/api/citation/feedback` | POST | Submit thumbs up/down feedback |

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Browser       │────▶│   Flask App      │────▶│   Supabase      │
│   (Dark UI)     │◀────│   (Python)       │◀────│   (Cache)       │
└─────────────────┘     └────────┬─────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌──────────────────┐
                        │   Grok-4 + Web   │
                        │   Search Tool    │
                        └──────────────────┘
```

**Flow:**
1. User clicks citation `[33]`
2. Check in-memory cache → Supabase cache
3. If miss: Call Grok-4 with web search to analyze source
4. Return structured response: `{title, relevant_quote, summary}`
5. Cache result for future requests

## Project Structure

```
grokipedia/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
└── templates/
    ├── index.html      # Home page with search
    ├── article.html    # Article view with citation UI
    └── error.html      # Error page
```

## Known Limitations

- Some paywalled sources can't be fully analyzed
- Very long articles may have many citations to process
- First-time citation lookups take 5-15 seconds (cached after)

## Credits

- [Grokipedia](https://grokipedia.com) for the source content
- [xAI](https://x.ai) for Grok API and web search
- Built for the xAI Hackathon 2025
