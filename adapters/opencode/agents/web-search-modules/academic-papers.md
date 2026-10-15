---
description: web-search 場景策略模組：供 web-search agent 讀取，不是 agent
hidden: true
---

# Academic Papers Module

**触发场景**: 论文查找、学术研究、算法原理

## 搜索源 (Academic Sources)
- **Google Scholar** (scholar.google.com) - comprehensive academic search engine
- **arXiv** (arxiv.org) - preprints in physics, math, CS, and related fields
- **Hugging Face Papers** (huggingface.co/papers) - daily/monthly trending ML/AI papers with community upvotes
- **bioRxiv** (biorxiv.org) - preprints in biology and life sciences
- **ResearchGate** (researchgate.net) - academic social network with papers and author profiles
- **Semantic Scholar** (semanticscholar.org) - AI-powered academic search
- **ACM Digital Library** and **IEEE Xplore** - CS and engineering papers

## arXiv 結構化檢索 (Structured arXiv Search，優先路徑)

**前提**：環境已安裝 `academic-paper-search` skill（本 repo 的 `skills/academic-paper-search/`）。
未安裝時**不要假設它存在**，直接走下方「搜索源」與「查询策略」的網路搜尋流程。

- 涉及 arXiv 的論文查找（關鍵字、作者、分類、日期範圍）**優先走這條結構化路徑**，
  再輔以下方的網路來源：
  1. 先用 `skill` 工具載入 `academic-paper-search`（ID 即 `academic-paper-search`）。
  2. 依該 skill 的 `SKILL.md` 執行 `scripts/arxiv_search.py`，例如
     `python3 scripts/arxiv_search.py --query "all:agent memory" --max-results 10 --format json`。
  3. 取回結構化結果（標題、作者、摘要、arXiv ID、分類、連結、`total_results`）；
     需要引用格式時改用 `--format bibtex`。
- 用途：省去人工翻頁，先取得可核對的 arXiv ID／作者／分類等欄位，再回 arXiv 頁面或 PDF 核對全文。
- **界線**：此路徑只涵蓋 arXiv 全庫檢索。熱門論文與社群訊號仍看 Hugging Face Papers
  （若環境另裝有 `hf-cli` skill，可用它的 `hf papers info/list/search/read`）；
  Google Scholar、bioRxiv、Semantic Scholar、ResearchGate、ACM DL、IEEE Xplore
  一律照下方原有策略走網路搜尋。

## 查询策略 (1.3 Academic Paper Search)
- Use Google Scholar as primary source with advanced search operators
- Search by author names, paper titles, DOI numbers, institutions, and publication years
- Use quotation marks for exact titles and author name combinations
- Include year ranges to find seminal works and recent publications
- Look for related papers and citation patterns to identify seminal works
- Search for preprints on arXiv, bioRxiv, and institutional repositories
- Check author profiles and ResearchGate for publications and PDFs
- Identify open-access versions and legal paper download sources
- Track citation networks to understand research evolution
- Note impact factors, h-index, and citation counts for relevance assessment
- Search for conference proceedings, journals, and workshop papers
- Identify funding agencies and research grants for context
