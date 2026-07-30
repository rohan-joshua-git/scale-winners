"""One-off script: clean raw HTML/PDF fetches into the 6 final plain-text source
documents for Phase 1 ingestion. Not part of the pipeline itself -- run once,
outputs land in data/regulatory_docs/*.txt."""
import re
import html
from pathlib import Path

import pypdf

RAW = Path(__file__).parent
OUT = RAW.parent


def clean_html(raw: str) -> str:
    # FATF pages embed body copy as JSON-escaped strings inside AEM component
    # attributes (\r\n, <p> tags, escaped quotes) -- unescape those first.
    raw = raw.replace("\\r\\n", "\n").replace('\\"', '"')
    raw = re.sub(r'<(script|style|nav|footer|noscript)[^>]*>.*?</\1>', '', raw, flags=re.S | re.I)
    raw = re.sub(r'<!--.*?-->', '', raw, flags=re.S)
    raw = re.sub(r'</(p|div|li|h[1-6]|br|tr)>', '\n', raw, flags=re.I)
    raw = re.sub(r'<li[^>]*>', '- ', raw, flags=re.I)
    text = re.sub(r'<[^>]+>', '', raw)
    text = html.unescape(text)
    # AEM double-encodes some inner markup (HTML entities inside a JSON string
    # inside an HTML attribute) -- after unescaping, real tags can reappear, so
    # strip tags a second time.
    text = re.sub(r'</?(p|div|li|h[1-6]|br|tr|span)[^>]*>', ' ', text, flags=re.I)
    # Strip leftover AEM JSON artifacts like `"}}" id="text-..." class="cmp-text">`
    text = re.sub(r'"\}\}"\s*id="[^"]*"\s*class="[^"]*">', '', text)
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and l != '-']
    out, prev = [], None
    for l in lines:
        if l != prev:
            out.append(l)
        prev = l
    return "\n".join(out)


def clean_pdf(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def write_doc(name: str, header: str, sections: list[tuple[str, str]]):
    parts = [header, ""]
    for title, body in sections:
        parts.append(f"\n{'=' * 10} {title} {'=' * 10}\n")
        parts.append(body.strip())
    (OUT / name).write_text("\n".join(parts), encoding="utf-8")
    print(f"{name}: {sum(len(b) for _, b in sections):,} chars")


# --- FATF grey list ---
grey_full = clean_html((RAW / "fatf_grey.html").read_text(encoding="utf-8", errors="replace"))
# Trim leading nav junk -- body starts at the "Jurisdictions under increased monitoring" paragraph
idx = grey_full.find("Jurisdictions under increased monitoring are actively working")
grey_body = grey_full[idx:] if idx != -1 else grey_full
write_doc(
    "fatf_grey_list.txt",
    "FATF Jurisdictions under Increased Monitoring (\"Grey List\") -- 19 June 2026\n"
    "Source: https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/increased-monitoring-june-2026.html",
    [("Grey list statement", grey_body)],
)

# --- FATF black list ---
black_full = clean_html((RAW / "fatf_black.html").read_text(encoding="utf-8", errors="replace"))
idx = black_full.find("High-Risk Jurisdictions subject to a Call for Action")
black_body = black_full[idx:] if idx != -1 else black_full
write_doc(
    "fatf_black_list.txt",
    "FATF High-Risk Jurisdictions subject to a Call for Action (\"Black List\") -- 19 June 2026\n"
    "Source: https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/call-for-action-june-2026.html",
    [("Call for Action statement", black_body)],
)

# --- OFAC: overview + per-program pages ---
ofac_sections = [("Program list overview (home.treasury.gov)",
                   clean_html((RAW / "ofac_overview.html").read_text(encoding="utf-8", errors="replace")))]
for label, fname in [
    ("Iran Sanctions", "ofac_iran.html"),
    ("North Korea (DPRK) Sanctions", "ofac_dprk.html"),
    ("Russian Harmful Foreign Activities Sanctions", "ofac_russia.html"),
    ("Cuba Sanctions", "ofac_cuba.html"),
    ("Venezuela-Related Sanctions", "ofac_venezuela.html"),
    ("Syria / Promoting Accountability for Assad and Regional Stabilization Sanctions (PAARSS)", "ofac_syria.html"),
]:
    body = clean_html((RAW / fname).read_text(encoding="utf-8", errors="replace"))
    idx = body.find("Sanctions Programs and Country Information")
    ofac_sections.append((label, body))
write_doc(
    "ofac_sanctions_programs.txt",
    "OFAC Sanctions Programs -- SDN List Summary + Program Pages (DPRK, Iran, Russia, Cuba, Venezuela, Syria)\n"
    "Source: https://ofac.treasury.gov/sanctions-programs-and-country-information",
    ofac_sections,
)

# --- EU: AML package article + Commission policy page ---
eu_article = clean_html((RAW / "eu_amld_article.html").read_text(encoding="utf-8", errors="replace"))
eu_fisma = clean_html((RAW / "eu_fisma_aml.html").read_text(encoding="utf-8", errors="replace"))
write_doc(
    "eu_amld_sanctions.txt",
    "EU Consolidated Sanctions List + AMLD5/AMLD6 Key Provisions\n"
    "Sources: https://financialregulations.eu/blog/eu-aml-package-amla-amlr-guide ;"
    " https://finance.ec.europa.eu/financial-crime/anti-money-laundering-and-countering-financing-terrorism-eu-level_en",
    [("EU AML Package guide (AMLD6/AMLR/AMLA)", eu_article),
     ("European Commission -- AML/CFT at EU level", eu_fisma)],
)

# --- Wolfsberg CBDDQ Guidance (PDF) ---
wolfsberg_pdf_text = clean_pdf(RAW / "wolfsberg_cbddq_guidance.pdf")
write_doc(
    "wolfsberg_guidance.txt",
    "Wolfsberg Group -- Correspondent Banking Due Diligence Questionnaire (CBDDQ) Guidance v2.0 (2023)\n"
    "Source: https://db.wolfsberg-group.org/assets/09bafec3-4b77-428b-9113-f4203d290a2f/EN_CBDDQ%20Guidance%20(2023).pdf",
    [("CBDDQ Guidance", wolfsberg_pdf_text)],
)

# --- MAS Notice 626 (PDF, base + 2025 amendment) ---
mas_base = clean_pdf(RAW / "mas_notice_626.pdf")
mas_amend = clean_pdf(RAW / "mas_notice_626_amendment_2025.pdf")
write_doc(
    "mas_notice_626.txt",
    "MAS Notice 626 -- Prevention of Money Laundering and Countering the Financing of Terrorism (Banks)\n"
    "Base: dated 28 Mar 2024, in effect from 1 Apr 2024. Amendment: dated 30 Jun 2025, in effect from 1 Jul 2025.\n"
    "Source: https://www.mas.gov.sg/regulation/notices/notice-626",
    [("Notice 626 (28 Mar 2024 base text)", mas_base),
     ("Notice 626 Amendment (30 Jun 2025)", mas_amend)],
)

print("\nDone.")
