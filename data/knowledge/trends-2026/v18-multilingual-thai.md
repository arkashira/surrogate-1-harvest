---
title: "V18 — Multilingual + Thai-Native Surrogate-1"
date: 2026-05-01
tags: [trends-2026, surrogate-1, v18, thai, multilingual, training, datasets]
status: research-complete
priority: P0 (owner-language)
related:
  - "[[v15-frontier-late-2025]]"
  - "[[v16-bleeding-edge-may2026]]"
  - "[[training-tooling-2026-Q2]]"
  - "[[opensource-releases-2026-Q2]]"
---

# V18 — Multilingual + Thai-Native Surrogate-1

## TL;DR

| Question | Answer |
|----------|--------|
| Best base for Thai-native? | **Qwen3-7B** (119 langs, 250k vocab, native Thai BPE) > GLM-5 (CN/EN heavy) > Granite (no Thai) > Qwen2.5-Coder-7B (current) |
| Cheapest path to Thai fluency? | **Continued pretrain on Mangosteen 47B + Typhoon 2 SFT mix + DPO with cross-lingual rewards** (~$2-3k for 7B on 8xA100) |
| Top Thai dataset (commercial-clean)? | **Mangosteen-47B** (Apache-2.0) + **OpenThaiGPT 2M-pairs** (Apache-2.0) + **Wisesight** (CC0) |
| Tokenizer fix? | Qwen3 already has 250k vocab w/ Thai-optimized BPE — no extension needed. For Qwen2.5-Coder/Granite → run **VRCP** (Vocabulary Replacement Continued Pretraining) |
| Eval? | **ThaiExam + ThaiLLM-Leaderboard + Thai-H6 + ThaiCLI + ThaiSafetyBench** — already SeaCrowd-hosted |

**Decision for V18**: Switch base to **Qwen3-7B-Instruct** (or Qwen3-8B if VRAM permits). Thai becomes natively supported with no tokenizer surgery. Add Thai SFT/DPO mix on top of code-mix from current V17.

---

## 1. Thai-Specific Models (2024-2026)

### 1.1 Typhoon Series (SCB10X) — State of the Art for Thai

| Model | Base | Date | License | Take |
|-------|------|------|---------|------|
| Typhoon-7B | Mistral-7B | Dec 2023 | Apache-2.0 | First strong open Thai LLM |
| Typhoon-1.5x-70B | Llama-3-70B | Jul 2024 | Llama-3 license | Production-grade |
| **Typhoon 2** (1B/3B/8B/70B) | Llama-3.1 + Gemma-3 12B variant | Dec 2024 | Llama / Gemma | **Current SOTA Thai** — IFEval-TH best-in-class, 128k context |
| **Typhoon T1 3B** | Typhoon 2 3B | Feb 2025 (ICLR SCI-FM) | Apache-2.0 | Open Thai *reasoning* model — full data recipe published. SFT-only (no RL). HF: `scb10x/typhoon-t1-3b-sci-fm-iclr-2025-exp-dataset` |
| Typhoon2-Audio | Typhoon 2 + audio encoder | 2025 | Apache-2.0 | Speech-in/speech-out — useful for owner's voice workflow later |
| Typhoon2-DeepSeek-R1-70B preview | merged | 2025 | Llama-3.1 | Reasoning + Thai fusion |

**Take for Surrogate-1**: Typhoon 2 SFT mix is the gold standard for Thai instruction. Use as "external SFT corpus" via `merge_external()`. Don't use Typhoon 2 *base weights* (Llama license restrictions) — but its **SFT data, training recipe, and benchmark setup** are the playbook.

### 1.2 OpenThaiGPT (AIEAT)

| Version | Base | Date | License | Notes |
|---------|------|------|---------|-------|
| OpenThaiGPT 1.0 (70B) | Llama-2-70B | 2023 | Llama-2 | Vocab extended for Thai |
| OpenThaiGPT 1.5 (7B/14B/72B) | Qwen2.5 | Sep 30, 2024 | Apache-2.0 | **2M Thai instruction pairs** corpus released |
| **OpenThaiGPT 1.6 + R1 (32B)** | Qwen2.5 | Mar 31, 2025 (arXiv 2504.01789) | Apache-2.0 | OTG-1.6 = task-arithmetic merge; OTG-R1 = LIMO multi-stage reasoning. **R1 beats DeepSeek-R1 + Typhoon2-R1 at 32B**. LiveCodeBench-TH 32.43 vs 12.61 prior. |

**Take**: OpenThaiGPT 1.6 R1 paper is essential reading — it's the open recipe for "Thai + reasoning + code" at our exact tier. Their 2M-pair instruction corpus is the **largest commercially-clean Thai SFT set on HF**. Apache-2.0 means we can use weights AND data.

### 1.3 Pathumma LLM (NECTEC / NSTDA)

| Model | HF | License | Notes |
|-------|----|---------|------|
| Pathumma-llm-text-1.0.0 | `nectec/Pathumma-llm-text-1.0.0` | (check) | Government-sponsored, Thai-context tuned |
| Pathumma-llm-vision-1.0.0 | `nectec/Pathumma-llm-vision-1.0.0` | (check) | Multimodal |
| Pathumma-llm-audio-1.0.0 | `nectec/Pathumma-llm-audio-1.0.0` | (check) | Audio |

NECTEC plans larger Foundation Model and Agentic AI by 2025. **Note**: SambaNova partnership rumor — not confirmed in 2026 sources. Pathumma is NOT released by SambaNova; it's NECTEC.

**Take**: Useful as Thai cultural alignment reference. Smaller weight in mix because government/cultural data may carry implicit alignment we don't want for Surrogate-1's coder persona.

### 1.4 OpenJAI (JTS-AI) — newest Thai model

- **Paper**: arXiv 2510.06847 (Oct 2025)
- **Base**: Qwen3-14B
- **HF**: `JTS-AI/OpenJAI-v1.0`
- **Training**: 462M tokens, batch 256, **<1 day training** — very efficient
- **Focus**: instruction-following + long-context + tool use (THREE pillars also Surrogate-1 cares about)
- **License**: Open (Qwen3 base)

**Take**: This is the closest analog to V18's goal. Read their data curation methodology — they prove you can hit SOTA Thai with 462M tokens by **quality not quantity**. Apply same principle.

### 1.5 JAI-1 (separate paper)

- arXiv 2510.08620 (Oct 2025)
- "Thai-Centric Large Language Model"
- Different team from OpenJAI — overlapping space

### 1.6 Thai-Mistral / Thai-Llama / Thai-Qwen (community)

Community fine-tunes mostly subsumed by Typhoon (Mistral-based) and OpenThaiGPT (Qwen-based). No standalone "Thai-Mistral" project that beats Typhoon. **Skip in mix** — use the curated ones above instead.

### 1.7 WangchanBERTa / WangchanLM (VISTEC)

- **WangchanBERTa**: 78GB Thai pretrain, RoBERTa-base — encoder-only, useful for embedding/classification heads only, NOT for generation. arXiv 2101.09635.
- **WangChanGLM**: multilingual instruction-following, CC-BY-SA 4.0, ~400k examples (LAION OIG, Dolly v2, OpenAI TL;DR, HC3) — **commercially permissible**.
- **WangchanThaiInstruct (2025)**: arXiv 2508.15239, ACL Anthology 2025.emnlp-main.175. 4 professional domains × 7 task types. 30k samples CC-BY-NC + 5,014 CC-BY-SA-4.0 (commercial-clean subset).

**Take**: Use the 5,014 CC-BY-SA subset of WangchanThaiInstruct for **culture-aware instruction tuning**. Apply WangchanBERTa as Thai-text similarity filter for data dedup, not as a generation base.

---

## 2. Multilingual Frameworks Worth Comparing

### 2.1 Qwen3 (Alibaba) — TOP PICK FOR V18 BASE

- **119 languages and dialects** (up from 29 in Qwen2.5)
- **250k vocab** (up from 150k) — comparable to Gemini's ~256k
- Larger vocab → **15-40% fewer tokens for Thai/Arabic/Korean/Japanese/Hindi**
- Byte-level BPE = lossless multilingual + code
- Sizes: 0.6B/1.7B/4B/8B/14B/32B + Qwen3-Coder series (Jul 2025) + Qwen3.5 (Q4 2025)
- Qwen3.6:35B-A3B (MoE, Apr 2026) — 24GB MoE, SWE-Bench 73.4%

**Why Qwen3 wins for V18**:
1. Native Thai already in tokenizer — **no VRCP needed**
2. SWE-Bench 76% (Qwen3.5:27B class) — beats Qwen2.5-Coder-7B
3. Apache-2.0 license — commercial-clean
4. Owner already runs Ollama with Qwen3 family — toolchain ready
5. OpenThaiGPT 1.6/R1 + OpenJAI both built on Qwen3 (or Qwen2.5) — recipes transfer

### 2.2 GLM-5 / GLM-5.1 (Z.ai / Zhipu)

- GLM-5: Feb 11, 2026 — 355B / 744B MoE (32B / 40B active), 28.5T tokens
- GLM-5.1: Apr 8, 2026 — agentic SOTA, SWE-Bench Pro #1
- **Multilingual**: EN + CN heaviest. "Many languages but EN/CN strongest"
- **Thai**: not advertised; weaker than Qwen3
- Vocab: optimized for CN, decent for Thai but lower than Qwen3

**Take for V18**: NOT recommended as base. GLM-5 = best for Chinese-centric multilingual; for Thai-native, Qwen3 wins. Reserve GLM-5 as *teacher* for distillation if needed.

### 2.3 IBM Granite 4.1

- **Granite 4.1 base**: EN, DE, ES, FR, JA, PT, AR, CS, IT, KO, NL, ZH (12 langs)
- **Thai NOT supported** in main models — only in **Granite-Embedding-R2** (52 langs incl. Thai)
- Size: 8B base, MoE variants

**Take**: Skip Granite for Thai base. Use Granite Embedding R2 for **Thai retrieval / dedup** in data pipeline, not as the gen model.

### 2.4 Aya Expanse 2 (Cohere Labs)

- 8B + 32B, **23 languages**, CC-BY-NC license (research only)
- Languages: AR, ZH (S+T), CS, NL, EN, FR, DE, EL, HE, HI, ID, IT, JA, KO, FA, PL, PT, RO, RU, ES, TR, UK, VI
- **Thai NOT in the 23** — Aya focused on Vietnamese/Indonesian for SEA
- Aya-32B beats Gemma-2-27B and Llama-3.1-70B on multilingual

**Take**: Useful as **multilingual SFT data source** (CC-BY-NC OK if we keep weights private). Their data arbitrage + multilingual preference training papers are essential reading for V18 DPO.

### 2.5 SEA-LION v4 (AI Singapore)

- Multimodal (Aug 2025), 256k context
- Bases: Gemma-3-27B, Qwen-3 (8B-VL, 32B-IT)
- 11 SEA langs incl. **Thai, Burmese, Lao, Khmer, Tagalog, Vietnamese, Indonesian, Malay, Tamil**
- Post-training: 10M QA pairs across 10 SEA langs
- SEALD (Southeast Asian Languages in One Network Data) — public training data
- **#5 of 55 globally on SEA tasks**

**Take**: **Best multilingual SEA mix data** — pull SEALD as our SEA-language SFT supplement. Don't use as base (Gemma license + multilingual dilution).

### 2.6 SeaLLMs 3 (DAMO/Alibaba)

- arXiv 2407.19672 / NAACL Demo 2025
- 12 SEA langs incl. Thai, Tagalog, Burmese, Khmer, Lao, Tamil, Javanese
- Cost-efficient via "specially constructed instruction tuning dataset"

### 2.7 EXAONE 4 / K-EXAONE (LG AI)

- EXAONE 4.0 (Jul 2025) — EN + KO + ES (limited multilingual)
- K-EXAONE — KO/EN/ES/DE/JA/VI (6 langs, MoE 236B/23B active)
- **Thai NOT supported**. Skip.

### 2.8 Swallow / Sarashina / OpenCALM (Japanese)

- Llama-3.3 Swallow (Tokyo Tech + AIST) — Japanese sovereign LLM, AWS HyperPod-trained
- Swallow Instruct v0.5 — JA dialogue + code SFT focus
- Sarashina2.2-3b-instruct (SB Intuitions)
- OpenCALM (CyberAgent)

**Take**: Pull JA SFT mix from Swallow for V18's Japanese leg. License = Llama-3 derivative (non-commercial restrictions in some).

---

## 3. Thai Datasets — Top 8 for V18 (Commercial-Clean)

| # | Dataset | HF | License | Tokens / Size | Take | Mix Weight |
|---|---------|----|---------|----------------|------|------------|
| 1 | **Mangosteen** | `vistec-AI/Mangosteen` | Apache-2.0 (code) + permissive corpus | **47B Thai tokens** | Cleanest large-scale Thai pretrain corpus. Thai-adapted Dolma pipeline. arXiv 2507.14664 (Jul 2025) | **35%** of Thai pretrain |
| 2 | **OpenThaiGPT 2M-Pairs** | embedded in `openthaigpt/openthaigpt-1.5/r1` repos | Apache-2.0 | 2M instruction pairs | Largest commercial-clean Thai SFT. Verified by Thai speakers. | **30%** of Thai SFT |
| 3 | **mC4-Thai** | `legacy-datasets/mc4` (lang=th) | ODC-BY | ~11B Thai tokens | Common Crawl-derived. Quality OK after cleaning. | **20%** of Thai pretrain |
| 4 | **CC-100 Thai** | `statmt/cc100` (lang=th) | CommonCrawl ToU | ~11B Thai tokens (XGLM used) | Older; complement to mC4. | **10%** of Thai pretrain |
| 5 | **HPLT v2 Thai** | HPLT project | CC0 | (large; check exact size) | 2025 expanded multilingual. arXiv 2403.14009 + ACL 2025.acl-long.854. Includes Internet Archive crawls. | **15%** Thai pretrain |
| 6 | **Wisesight Sentiment** | `pythainlp/wisesight_sentiment` + `SEACrowd/wisesight_thai_sentiment` | **CC0 (public domain)** | 26.7k labeled msgs | Social media sentiment — informal Thai (พี่/มึง/กู style register) | SFT classification + style anchor |
| 7 | **WangchanThaiInstruct (CC-BY-SA subset)** | `airesearch/wangchan-thai-instruction-*` (5,014 samples) | CC-BY-SA 4.0 | 5,014 high-quality | 4 pro domains × 7 task types. Culture-aware. arXiv 2508.15239 | **15%** of Thai SFT (high-quality boost) |
| 8 | **Typhoon T1 SFT** | `scb10x/typhoon-t1-3b-sci-fm-iclr-2025-exp-dataset` | Apache-2.0 | (3B-target sized) | Open Thai reasoning data — the **only fully-open Thai reasoning recipe** | **20%** of Thai reasoning SFT |

### Supporting / Optional

| Dataset | HF | License | Note |
|---------|----|---------|------|
| Thai Wikipedia dump | `wikipedia` (lang=th) | CC-BY-SA | Standard. ~150M tokens. Always include 5-10%. |
| Thai government corpus | `pythainlp/thaigov-corpus` | (check) | Formal register balance |
| Thai NER v2 | `pythainlp/thainer-corpus-v2` | (check) | NER auxiliary head only |
| Thai Literature (TLC) | `pythainlp/tlcv2.0_oa` | Public domain (old books) | Classical/formal Thai |
| LST20 | `lst-nectec/lst20` | CC-BY (research) | Tagged Thai corpus |
| SEACrowd Thai instruction (gpteacher) | `SEACrowd/thai_gpteacher` | (check derivative) | Translated instructions |
| SEACrowd Thai Dolly | `SEACrowd/thai_databricks_dolly` | Apache-2.0 (Dolly base) | Dolly-translated |
| ThaiCLI / Thai-H6 (eval, not train) | `UpstageAI/ThaiCLI_H6` | (research) | EVAL ONLY |
| Krathu-500 (Pantip-style) | TechRxiv preprint | (check, Pantip-derived) | Pantip post+comment corpus — may have ToU concerns |

**Pantip caveat**: Pantip ToS likely restricts commercial scraping. Krathu-500 is academic. **Don't include unless explicit license clear.** Use forum-style data from Wisesight + Typhoon's curated dialogue mix instead.

---

## 4. Tokenizer Recommendation

### 4.1 The Choice Matrix

| Base | Vocab | Thai Native? | Thai Tokens/Word | Recommended Action |
|------|-------|--------------|------------------|---------------------|
| **Qwen3 (8B/14B)** | 250k | **YES** | Low (efficient) | Use as-is. Zero tokenizer surgery. **WIN.** |
| Qwen2.5-Coder 7B (current Surrogate-1) | 152k | Partial (byte-fallback) | High (~3-5 tokens/Thai word) | VRCP or extend +20k Thai tokens |
| GLM-5 / GLM-5.1 | ~150k | EN+CN focused | Medium-High | Not recommended for Thai |
| Granite 4.1 | ~50k | NO Thai in main models | Very High | Skip for Thai |
| Llama-3 (Typhoon base) | 128k | Partial | High | Llama-license restricted |

### 4.2 Recommendation

**Switch to Qwen3-7B/8B as V18 base.** Tokenizer is solved.

If staying on Qwen2.5-Coder-7B (legacy reason), apply **VRCP (Vocabulary Replacement Continued Pretraining)** — ACL 2025 SUMEVAL workshop paper (`aclanthology.org/2025.sumeval-2.5.pdf`):
1. Train fresh BPE on 47B Mangosteen Thai → keep 20k most-efficient Thai tokens
2. Replace 20k least-used Qwen tokens → preserve vocab size
3. Continued pretrain 5-10B tokens to align embeddings

Cost: ~$1.5k extra on 8xA100 vs ~$0 if using Qwen3 directly.

### 4.3 Tokenizer Compatibility Detail

- **SentencePiece** treats text as raw stream → works for Thai (no whitespace)
- **Byte-level BPE** (Qwen3, Llama-3) → lossless, handles Thai script natively
- **WordPiece (BERT family)** → fails on Thai without preprocessing
- **For Thai script specifically**: bytes-fallback in BPE means *any* Thai char encodes, but **fertility matters**. Goal: <2 tokens per Thai syllable.

---

## 5. Training Techniques (V18 Recipe)

### 5.1 Stage 1 — Continued Pretraining (Thai weight injection)

```yaml
base: Qwen/Qwen3-7B-Instruct  # or Qwen3-8B
data_mix:
  thai:
    Mangosteen: 0.35  # 47B tokens, deep clean
    mC4-Thai: 0.20
    HPLT-v2-Thai: 0.15
    CC100-Thai: 0.10
    Thai-Wikipedia: 0.07
    PyThaiNLP-collections: 0.13  # gov, lit, NER
  multilingual:
    Aya-Expanse-data: 0.10  # ZH/JA/KO/ES/DE/FR
    SEALD: 0.05  # SEA breadth
  english:
    keep_existing_v17_distribution: 0.30  # don't lose code skill
target_tokens: 20-30B (1-2 epochs over Mangosteen-equivalent)
schedule: cosine, peak LR 2e-5, warmup 1%
hardware: 8xA100-80GB ~ 60-80hr ~ $2.5k-3.3k @ AWS p4de
```

**Critical**: Per "Revisiting Multilingual Data Mixtures" — no curse of multilinguality up to 400+ languages **as long as each lang ≥1-2B tokens**. Thai gets ~15B in our mix → fine.

### 5.2 Stage 2 — Multilingual SFT

```yaml
sft_mix:
  thai:
    OpenThaiGPT-2M-pairs: 0.30
    Typhoon2-style-instructions: 0.15  # from public Typhoon recipes
    WangchanThaiInstruct-CC-BY-SA: 0.05  # 5k high-quality
    Typhoon-T1-reasoning: 0.10
    Wisesight-sentiment-as-instructions: 0.05
  english:
    keep_v17_code_sft: 0.25
  other_langs:
    Aya-Expanse-mix: 0.05  # ZH/JA/KO/ES/DE/FR sample
    SEACrowd-thai-dolly: 0.05
total: ~3-5M samples
epochs: 3
context: 32k (start), expand to 128k after
```

### 5.3 Stage 3 — Cross-Lingual DPO

Apply **Implicit Cross-Lingual Rewarding** (arXiv 2503.04647):
1. Take EN-aligned model (already Surrogate-1 V17) as reward signal
2. Generate Thai responses, score using EN reward model on translated/embedding-aligned space
3. DPO with cross-lingual preference pairs

Expected gain (per paper): +12.7% Win Rate, +5.97% Length-Control Win Rate over languages.

Plus **MPO (Multilingual Safety Alignment via Reward Gap Optimization)** — arXiv 2505.16869.

### 5.4 Code-Switching Data

Per ACL 2025 paper "Code-Switching Curriculum Learning for Multilingual Transfer in LLMs":
- Generate **Thai-English code-switched sequences** synthetically (50/50 bilingual SFT subset)
- Insert English code blocks in Thai explanations (matches owner's actual usage: Thai prose + English variable names)
- Curriculum: monolingual → 25% mix → 50% mix → free-form

```python
# Code-switch sample template
"""user: ช่วยเขียน function Python ที่ filter list of dicts โดย key='status' == 'active' ให้หน่อย
assistant: ได้ครับ นี่คือ function:

```python
def filter_active(items: list[dict]) -> list[dict]:
    return [x for x in items if x.get('status') == 'active']
```

อธิบาย: ใช้ list comprehension แทน filter() เพราะอ่านง่ายกว่า ถ้า items ใหญ่มากให้ใช้ generator แทน"""
```

This pattern is in Wisesight + Typhoon — owner's natural register.

### 5.5 Translation as Supervision

Self-Translate-Train (arXiv 2407.00454):
1. Take strong EN code corpus (HumanEval, MBPP, our V17 SWE-Gym)
2. Self-translate to Thai using current model
3. Filter via roundtrip BLEU + LLM-judge
4. Add as bilingual SFT — **doubles effective training signal**

### 5.6 Cultural Alignment (Thai-Specific)

Per ThaiCLI + ThaiSafetyBench:
- **Pronoun system**: train on owner's preferred register (พี่/มึง/กู informal). Add few-shot examples in system prompt.
- **Avoid royal/political bias**: ThaiSafetyBench has these — use as **negative-DPO** (model should refuse politely on royal topics, not opine).
- **Buddhist context**: light touch — Pathumma corpus has examples but don't over-index.
- **Politeness levels**: model should match user register (informal-in → informal-out, formal-in → formal-out).

---

## 6. Concrete `merge_external()` Patch

```python
# ~/develope/AI/surrogate-1/training/v18/data_mixer.py

from datasets import load_dataset, concatenate_datasets, interleave_datasets
from typing import Literal

V18_THAI_MIX = {
    # PRETRAIN STAGE
    "pretrain": {
        "vistec-AI/Mangosteen": {"weight": 0.35, "split": "train", "license": "Apache-2.0"},
        "legacy-datasets/mc4": {"weight": 0.20, "config": "th", "license": "ODC-BY"},
        # HPLT requires direct download; sync to s3://surrogate-1-data/hplt-v2-th/
        "hplt-v2-th": {"weight": 0.15, "path": "s3://surrogate-1-data/hplt-v2-th/", "license": "CC0"},
        "statmt/cc100": {"weight": 0.10, "config": "th", "license": "CC-Net"},
        "wikipedia": {"weight": 0.07, "config": "20240801.th", "license": "CC-BY-SA"},
        "pythainlp/thaigov-corpus": {"weight": 0.05, "license": "verify"},
        "pythainlp/tlcv2.0_oa": {"weight": 0.04, "license": "PD-old-Thai"},
        "pythainlp/thainer-corpus-v2": {"weight": 0.04, "license": "verify"},
    },
    # SFT STAGE
    "sft": {
        "openthaigpt/openthaigpt-instruction-2M": {"weight": 0.30, "license": "Apache-2.0"},
        "scb10x/typhoon-t1-3b-sci-fm-iclr-2025-exp-dataset": {"weight": 0.10, "license": "Apache-2.0"},
        "airesearch/wangchan-thai-instruction-cc-by-sa": {"weight": 0.05, "n_samples": 5014, "license": "CC-BY-SA-4.0"},
        "pythainlp/wisesight_sentiment": {"weight": 0.05, "format": "as_instruction", "license": "CC0"},
        "SEACrowd/thai_databricks_dolly": {"weight": 0.05, "license": "Apache-2.0-derived"},
        "SEACrowd/thai_gpteacher": {"weight": 0.05, "license": "verify"},
    },
    # MULTILINGUAL BREADTH
    "breadth": {
        # Aya CC-BY-NC — research-only weights, don't ship to public model
        "CohereLabs/aya_collection": {"weight": 0.10, "license": "CC-BY-NC", "research_only": True,
                                      "subsets": ["zho", "jpn", "kor", "spa", "fra", "deu"]},
        "aisingapore/SEALD": {"weight": 0.05, "license": "verify"},
    },
    # KEEP V17 EN CODE
    "v17_en_code": {"weight": 0.30, "path": "internal://v17-sft-mix"},
}


def merge_external(stage: Literal["pretrain", "sft", "breadth"], v18_thai_mix: dict = V18_THAI_MIX):
    """V18 merge: load datasets, normalize schemas, interleave by weight.

    Returns IterableDataset. Pass to Trainer with dataset_text_field='text'.
    Filters: license check + dedup via SimHash on Thai n-grams.
    """
    sources = []
    weights = []
    for hf_id, cfg in v18_thai_mix[stage].items():
        if cfg.get("research_only") and not _is_research_run():
            continue  # skip CC-BY-NC for production runs
        ds = load_dataset(hf_id, name=cfg.get("config"), split=cfg.get("split", "train"),
                          streaming=True)
        if "format" in cfg and cfg["format"] == "as_instruction":
            ds = ds.map(_wisesight_to_instruction)
        if "n_samples" in cfg:
            ds = ds.take(cfg["n_samples"])
        ds = ds.map(_normalize_schema)
        sources.append(ds)
        weights.append(cfg["weight"])

    # Re-normalize weights to sum to 1
    total = sum(weights)
    weights = [w / total for w in weights]
    return interleave_datasets(sources, probabilities=weights, seed=42,
                              stopping_strategy="all_exhausted")


def _normalize_schema(ex):
    """Schemas vary across HF — unify to {text, lang, source, license}."""
    text = ex.get("text") or ex.get("content") or ex.get("instruction") or ""
    if "instruction" in ex and "output" in ex:
        text = f"<|user|>{ex['instruction']}<|assistant|>{ex['output']}"
    return {
        "text": text,
        "lang": ex.get("lang", "th"),
        "source": ex.get("__source__", "unknown"),
    }


def _wisesight_to_instruction(ex):
    """Wisesight: {text, category} → instruction format."""
    label_map = {0: "ลบ", 1: "เป็นกลาง", 2: "บวก", 3: "คำถาม"}
    return {
        "instruction": f"จัดประเภท sentiment ของข้อความนี้: {ex['text']}",
        "output": label_map.get(ex["category"], "เป็นกลาง"),
    }


def _is_research_run() -> bool:
    import os
    return os.getenv("SURROGATE_RESEARCH_MODE") == "1"
```

### Stage runner (slim):

```python
# ~/develope/AI/surrogate-1/training/v18/run_v18.py
from data_mixer import merge_external
from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments, Trainer

BASE = "Qwen/Qwen3-8B-Instruct"
tokenizer = AutoTokenizer.from_pretrained(BASE)
model = AutoModelForCausalLM.from_pretrained(BASE, torch_dtype="bfloat16", attn_implementation="flash_attention_2")

# Stage 1
pretrain_ds = merge_external("pretrain")
TrainingArguments(
    output_dir="./v18-stage1-pretrain",
    learning_rate=2e-5, warmup_ratio=0.01, lr_scheduler_type="cosine",
    per_device_train_batch_size=4, gradient_accumulation_steps=8,
    bf16=True, max_steps=15000,  # ~25B tokens
    save_steps=1000, logging_steps=50,
)
# Trainer.train() ...

# Stage 2
sft_ds = merge_external("sft")
# ... LoRA or full SFT, 3 epochs ...

# Stage 3 (DPO)
# Apply Implicit Cross-Lingual Rewarding via trl.DPOTrainer
```

---

## 7. Evaluation — Thai Benchmarks for `bench-v1-vs-v15.sh`

### 7.1 Add These to V18 Bench

| Benchmark | What | HF | Why |
|-----------|------|----|------|
| **ThaiExam** | MCQ from Thai high-school + investment exams (ONET) | `scb10x/thai_exam` | The Stanford HELM Thai standard |
| **ThaiLLM-Leaderboard suite** | 4 tasks × 10 datasets (Exam + NLU + LLM-judge + NLG) | github.com/scb10x/thai-llm-leaderboard | Most comprehensive |
| **Thai-H6** | 6 globalized benchmarks (MMLU/HellaSwag/etc.) translated to Thai | `UpstageAI/ThaiCLI_H6` | Core capability check |
| **ThaiCLI** | Cultural intelligence (royal/religion/culture/economy/humanity/lifestyle/politics) | `UpstageAI/ThaiCLI_H6` | Cultural alignment |
| **ThaiSafetyBench** | Thai cultural safety samples | arXiv 2603.04992 | Refusal calibration |
| **IFEval-TH** | Thai instruction-following | from Typhoon 2 paper | Match Typhoon target |
| **MT-Bench-TH** | Multi-turn Thai chat | Typhoon 2 release | Conversation quality |
| **M3Exam Thai** | Multilingual exam, Thai subset | M3Exam HF | Cross-lingual exam |
| **LiveCodeBench-TH** | Thai-prompted code | from OpenThaiGPT 1.6 paper | **Code in Thai** — critical for Surrogate-1 |
| **NitiBench** | Thai legal QA (optional) | arXiv 2502.10868 | Domain depth check |
| **Thai Dialect Bench** | Northern/Northeastern/Southern dialects | arXiv 2504.05898 | Bonus regional |

### 7.2 Bench Script Patch

```bash
# bench-v1-vs-v15.sh additions for V18
THAI_TASKS=(
  "thai_exam"               # MCQ
  "thaillm_leaderboard"     # Aggregate
  "thai_h6"                 # MMLU-class
  "thaicli"                 # Cultural
  "thai_safety_bench"       # Safety
  "ifeval_th"               # Instruction-follow
  "mt_bench_th"             # Multi-turn (uses GPT-4o-2024-05-13 as judge)
  "m3exam_th"               # Cross-lingual
  "livecodebench_th"        # Code-in-Thai
)

for task in "${THAI_TASKS[@]}"; do
  lm_eval \
    --model hf \
    --model_args "pretrained=${MODEL_PATH},dtype=bfloat16" \
    --tasks "${task}" \
    --batch_size 8 \
    --output_path "results/v18/${task}.json"
done

# Aggregate report
python scripts/thai_bench_summary.py results/v18/ \
  --baseline-models "scb10x/llama3.1-typhoon2-8b-instruct,openthaigpt/openthaigpt-r1-32b-instruct" \
  --output bench-v18-vs-typhoon-otg.md
```

---

## 8. Cost Summary (V18 Run on 8xA100-80GB)

| Stage | Tokens | Hours @ 5k tok/s | Cost @ $40.9/hr |
|-------|--------|-------------------|-----------------|
| Stage 1 — Continued pretrain | 25B | 56 hrs | **$2,290** |
| Stage 2 — SFT | 4M samples × ~500 tok = 2B | 6 hrs | **$245** |
| Stage 3 — DPO | 200k pairs × ~600 tok = 0.5B | 1.5 hrs | **$61** |
| Eval (full Thai bench) | — | 4 hrs | **$160** |
| **Total** | — | **~67 hrs** | **~$2,756** |

*If using Lightning H200 (faster) or Modal (spot): cut 30-50%. Kaggle TPU-v3 has Thai-data-friendly bandwidth but smaller VRAM.*

---

## 9. Decision Log for V18

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Base model | **Qwen3-7B/8B-Instruct** | 119 langs, 250k vocab w/ Thai-native, Apache-2.0, beats Qwen2.5 SWE |
| Tokenizer surgery? | **NO** | Qwen3 already has Thai BPE optimized |
| Top Thai dataset | Mangosteen + OpenThaiGPT-2M-pairs | Apache-2.0, 47B clean tokens + 2M instruction pairs |
| Skip | Pantip-scrape, Krathu-500 | License unclear |
| Cultural alignment via | ThaiCLI + ThaiSafetyBench (eval only, not train) | Avoid implicit gov/royal bias |
| Pronoun register | Owner's: พี่/มึง/กู mix informal | Sample 50 examples in system prompt + DPO preference data |
| Code-switching | **Mandatory 25% of Thai SFT** | Matches owner's natural usage |
| DPO method | Implicit Cross-Lingual Rewarding (2503.04647) | +12.7% Win Rate, no need for human Thai prefs |
| Eval runner | Add 9 Thai tasks to bench-v1-vs-v15.sh | Match SCB10X / OTG comparable |

---

## 10. References

### Models
- [Typhoon 2 (SCB10X)](https://opentyphoon.ai/blog/en/typhoon-2-release-9dd36e3882c0) — `scb10x/llama3.1-typhoon2-70b-instruct`
- [Typhoon T1 (arXiv 2502.09042)](https://arxiv.org/abs/2502.09042) — Thai reasoning
- [OpenThaiGPT 1.6 + R1 (arXiv 2504.01789)](https://arxiv.org/abs/2504.01789) — `openthaigpt/openthaigpt-r1-32b-instruct`
- [OpenThaiGPT 1.5 (arXiv 2411.07238)](https://arxiv.org/html/2411.07238v2)
- [OpenJAI v1.0 (arXiv 2510.06847)](https://arxiv.org/abs/2510.06847) — `JTS-AI/OpenJAI-v1.0`
- [JAI-1 (arXiv 2510.08620)](https://arxiv.org/html/2510.08620v1)
- [Pathumma (NECTEC)](https://huggingface.co/nectec) — `nectec/Pathumma-llm-text-1.0.0`
- [Qwen3](https://qwenlm.github.io/blog/qwen3/) — 119 langs, 250k vocab
- [Qwen3 Technical Report (arXiv 2505.09388)](https://arxiv.org/abs/2505.09388)
- [Aya Expanse (Cohere Labs)](https://huggingface.co/CohereLabs/aya-expanse-32b)
- [SEA-LION v4 (AI Singapore)](https://huggingface.co/aisingapore/Gemma-SEA-LION-v4-27B-IT)
- [SEA-LION (arXiv 2504.05747)](https://arxiv.org/abs/2504.05747)
- [SeaLLMs 3 (NAACL 2025)](https://aclanthology.org/2025.naacl-demo.10/)
- [GLM-5 (Z.ai)](https://huggingface.co/zai-org/GLM-5)
- [Granite 4.1 (IBM)](https://huggingface.co/ibm-granite/granite-4.1-8b)
- [EXAONE 4 / K-EXAONE (LG AI)](https://github.com/LG-AI-EXAONE/K-EXAONE)
- [Llama 3.3 Swallow](https://swallow-llm.github.io/llama3-swallow.en.html)

### Datasets
- [Mangosteen (arXiv 2507.14664)](https://arxiv.org/abs/2507.14664) — `vistec-AI/Mangosteen`
- [Wisesight Sentiment](https://huggingface.co/datasets/pythainlp/wisesight_sentiment)
- [WangchanThaiInstruct (arXiv 2508.15239)](https://arxiv.org/abs/2508.15239)
- [SEACrowd Thai datahub](https://github.com/SEACrowd/seacrowd-datahub)
- [HPLT v2 (ACL 2025)](https://aclanthology.org/2025.acl-long.854/)
- [PyThaiNLP collections](https://huggingface.co/pythainlp)
- [mC4](https://huggingface.co/datasets/legacy-datasets/mc4)
- [CC-100](https://huggingface.co/datasets/statmt/cc100)

### Benchmarks
- [ThaiExam Leaderboard (HELM)](https://crfm.stanford.edu/2024/09/04/thaiexam.html)
- [ThaiLLM Leaderboard (Typhoon)](https://opentyphoon.ai/blog/en/introducing-the-thaillm-leaderboard-thaillm-evaluation-ecosystem-508e789d06bf)
- [Thai-H6 + ThaiCLI](https://github.com/UpstageAI/ThaiCLI_H6)
- [Thai Dialect Bench (arXiv 2504.05898)](https://arxiv.org/abs/2504.05898)
- [NitiBench Thai legal (arXiv 2502.10868)](https://arxiv.org/html/2502.10868v1)
- [SEACrowd Benchmark (arXiv 2406.10118)](https://arxiv.org/html/2406.10118v1)

### Techniques
- [VRCP — Vocabulary Replacement Continued Pretraining](https://aclanthology.org/2025.sumeval-2.5.pdf)
- [Implicit Cross-Lingual Rewarding (arXiv 2503.04647)](https://arxiv.org/abs/2503.04647)
- [MPO Multilingual Safety (arXiv 2505.16869)](https://arxiv.org/html/2505.16869)
- [Self-Translate-Train (arXiv 2407.00454)](https://arxiv.org/html/2407.00454v2)
- [Code-Switching Curriculum (ACL 2025)](https://aclanthology.org/2025.findings-acl.407.pdf)
- [Middle-Layer Representation Alignment (ACL 2025)](https://aclanthology.org/2025.acl-long.778/)
- [Cross-lingual In-Context Pretraining (arXiv 2504.20484)](https://arxiv.org/html/2504.20484)

### Cultural / Safety
- [ThaiSafetyBench (arXiv 2603.04992)](https://arxiv.org/html/2603.04992)
- [Representing the Under-Represented Thai (arXiv 2410.04795)](https://arxiv.org/html/2410.04795v1)

---

## See Also

- [[v15-frontier-late-2025]] — Qwen3 base reasoning context
- [[v16-bleeding-edge-may2026]] — current frontier
- [[training-tooling-2026-Q2]] — DPO/KTO toolchain
- [[opensource-releases-2026-Q2]] — license check across releases
