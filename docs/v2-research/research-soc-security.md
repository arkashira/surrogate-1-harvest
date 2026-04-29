---
title: "Surrogate-1 v2: SOC Analyst & Security Engineer Capability Research"
date: 2026-04-29
session: surrogate1-honest-audit
focus: DevSecOps - SOC + Security Engineer + DFIR + Compliance
target-model: Qwen2.5-Coder-7B + LoRA → Surrogate-1 v2
tags: [security, soc, detection-engineering, threat-hunting, dfir, compliance, llm-security, research]
---

# Surrogate-1 v2: SOC Analyst & Security Engineer Capability Research

> Mission: Teach Surrogate-1 to be a SOTA DevSecOps engineer covering threat detection, IR,
> vuln management, compliance, secure code review, threat hunting, DFIR, red/purple teaming.
>
> Background: Qwen2.5-Coder-7B is the base. v1 added DevOps. v2 must add the **Sec** in
> "DevSecOps" — autonomous detect → investigate → respond → remediate.

---

## Executive Summary

The 2025–2026 security landscape collapsed the old "SOC Tier 1/2/3" pyramid. Vendors
(Microsoft Security Copilot, CrowdStrike Charlotte AI, SentinelOne Purple AI, Google Threat
Intelligence AI, Trend Cybertron) shipped agentic SOCs that triage 100% of alerts
autonomously, escalating only ambiguous cases. The bottleneck shifted from headcount to
**detection-engineering quality** and **AI agent governance**. CyberSOCEval (Meta+CrowdStrike,
Sept 2025) and CTI-Bench established the first credible SOC-LLM benchmarks. Open datasets
matured: Primus (10B+ cyber tokens, Trend Micro), CyberLLMInstruct (54.9k pairs), CTI-Bench,
SecBench (44k MCQs), SecEval, CyberMetric. The Llama-Primus-Nemotron-70B and similar
open-source security LLMs proved that domain pre-training + reasoning distillation gives
~16% lift on CISSP-level tasks.

For **Surrogate-1 v2** (Qwen2.5-Coder-7B + LoRA), the achievable target is a competent
**Tier 1.5–Tier 2 SOC analyst** with **strong detection engineering** + **secure code review**
+ **AWS cloud-security automation** at 8B parameters — not a Tier 3 hunter, not an autonomous
red-teamer. We aim for ≥75% on CyberMetric, ≥65% on CTI-Bench, ≥60% on SecBench English MCQ,
and credible Sigma/YARA/Falco rule generation under eval.

---

## 1. SOC Tier Roles + Capabilities (and how AI is flattening them)

### 1.1 Traditional Tier Structure (still used in regulated industries)

| Tier | Primary Role | Skills | Tools | Surrogate-1 fit |
|------|-------------|--------|-------|----------------|
| **T1 — Triage** | Alert ack, false-positive filter, basic enrichment, escalate | SIEM query, log reading, basic IOC lookup | Splunk SPL, Sentinel KQL, Elastic, EDR consoles | **YES** (primary target) |
| **T2 — Investigation / IR** | Validate scope, root cause, contain, IR playbook execution | Forensics, log timelines, attacker mindset, scripting | EDR, SIEM, SOAR, IR runbooks | **YES** (stretch goal) |
| **T3 — Hunt + Detection Engineering** | Hypothesis-driven hunts, malware RE, custom detection rules | Sigma/YARA authoring, Python scripting, ATT&CK fluency, RE | Sigma, YARA, Falco, Volatility, Ghidra | **PARTIAL** (rule generation + ATT&CK reasoning) |
| **SOC Manager / Engineer** | Architecture, tool selection, KPI design, hiring | Strategy, leadership, business context | All | **NO** (out of scope) |
| **DFIR Specialist** | Forensic acquisition, timeline reconstruction, court-ready evidence | Memory forensics, disk imaging, chain of custody | Volatility 3, Autopsy, FTK, KAPE | **PARTIAL** (Volatility command generation, timeline analysis) |
| **Threat Intel Analyst** | Adversary tracking, IOC curation, briefings | OSINT, MISP, STIX/TAXII, geopolitics | MISP, OpenCTI, ThreatConnect | **YES** (CTI-Bench targets this) |
| **Pen Tester / Red Team** | Adversary simulation, exploit dev | OSCP/OSEP skills, AD attack chains, exploit dev | Cobalt Strike, Sliver, Metasploit, BloodHound | **PARTIAL** (defensive understanding only — no offensive enablement) |
| **Purple Team / Detection Engineer** | Convert red findings → detections, ATT&CK coverage analysis | Sigma authoring, Atomic Red Team, validation | Atomic Red Team, Caldera, Sigma, ATT&CK Navigator | **YES** (key sweet spot) |
| **Compliance Engineer** | Map controls, evidence collection, audit prep | SOC2/ISO/NIST/PCI, control writing | Vanta, Drata, Secureframe | **YES** (control-mapping + evidence Q&A) |

### 1.2 Agentic SOC reality (2025–2026)

Average enterprise SOC: **4,484 alerts/day** from 28+ tools. Analyst spends 70 minutes per
alert. 56 minutes pass before any human looks at it. AI-driven attacks now move at speeds
**100× faster** than human-driven response. Both Gartner and Forrester retired their SOAR
evaluations in 2025 — the standalone SOAR category collapsed because **agentic AI dynamically
generates playbooks** instead of executing static ones.

Vendors shipping agentic SOCs (50+ as of 2026): Prophet Security, Dropzone AI, D3 Security,
Torq HyperSOAR, Trend Cybertron, CrowdStrike Charlotte, Microsoft Security Copilot agents,
SentinelOne Purple AI, Google Threat Intelligence AI, Conifers, Radiant Security.

**Implication for Surrogate-1**: We are not building a competitor to these. We are building
an **on-prem / open-weight DevSecOps assistant** that runs in user's own infra, integrates
with user's CDK/Terraform/GitHub Actions, and answers security questions grounded in user's
own runbooks + AWS account context. The competitive advantage is **codebase awareness**
(via the existing v1 RAG), **privacy** (never leaves user infra), and **DevOps-Sec fusion**
(one model reasons across `cdk-infrastructure/` AND `prowler-scan/`).

---

## 2. Detection Engineering — Sigma, YARA, EQL, KQL, SPL

### 2.1 Sigma (cross-SIEM standard)

Sigma is the **portable detection language** — write once, compile to Splunk SPL, Sentinel
KQL, Elastic EQL, QRadar AQL, Chronicle UDM, Sumo Logic, Panther, Datadog. Public repo
SigmaHQ has **3000+ rules** covering Windows / Linux / macOS / cloud / network. v18 ATT&CK
update (Oct 2025) introduced **Detection Strategies + Analytics** that pair tightly with
Sigma idioms.

**Real Sigma rule — Mimikatz / credential dumping (T1003):**

```yaml
title: Potential Invoke-Mimikatz PowerShell Script
id: 189e3b02-82b2-4b90-9662-411eb64486d4
status: test
description: Detects Invoke-Mimikatz PowerShell script and alike. Mimikatz is a
  credential dumper capable of obtaining plaintext Windows account logins and passwords.
references:
  - https://www.elastic.co/guide/en/security/current/potential-invoke-mimikatz-powershell-script.html
author: Tim Rauch, Elastic (idea)
date: 2022-09-28
tags:
  - attack.credential-access
  - attack.t1003
logsource:
  category: ps_script
  product: windows
detection:
  selection_1:
    ScriptBlockText|contains|all:
      - 'DumpCreds'
      - 'DumpCerts'
  selection_2:
    ScriptBlockText|contains: 'sekurlsa::logonpasswords'
  selection_3:
    ScriptBlockText|contains|all:
      - 'crypto::certificates'
      - 'CERT_SYSTEM_STORE_LOCAL_MACHINE'
  condition: 1 of selection*
falsepositives:
  - "Mimikatz can be useful for testing the security of networks"
level: high
```

**What Surrogate-1 must learn**:
- Sigma YAML schema (title, id, status, logsource, detection, condition, falsepositives, level)
- ATT&CK tag conventions (`attack.<tactic>`, `attack.t<id>`)
- Compile to backend queries via `sigma-cli` and `pySigma`
- Common selection patterns: `contains|all`, `endswith`, `re|i`, modifiers
- Anti-patterns: regex catastrophic backtracking, false-positive avalanche

**Sigma-genai-friendly tasks for training**:
1. Given an attack technique description → output Sigma rule
2. Given a Windows EVTX log → identify if any existing Sigma rule fires
3. Given a Sigma rule + log sample → predict true/false positive
4. Translate Sigma → Splunk SPL / Elastic EQL / Sentinel KQL
5. Improve a rule with low precision (add suppress / filter / context)

### 2.2 YARA (file/memory pattern matching)

YARA is the **malware classification language** — pattern matching on file content,
strings, byte sequences, PE/ELF structure. Used by VirusTotal, Yextend, every AV/EDR.
Elastic Security ships 1000+ YARA rules. ESXi-targeting Play ransomware (CISA advisory
June 2025) provides a real example.

**Example YARA — Play ransomware ESXi variant:**

```yara
rule Play_Ransomware_ESXi_Variant {
    meta:
        description = "Detects Play ransomware ESXi-targeting binary"
        author = "CISA"
        date = "2025-06"
        reference = "AA25-XXX-A Play Ransomware Update"
        hash = "<sha256>"
    strings:
        $vm_kill = "esxcli vm process kill --type=force"
        $datastore = "/vmfs/volumes/"
        $ransom_note = "ReadMeForDecrypt.txt"
        $play_marker = ".PLAY"
        $ext_vmdk = ".vmdk"
        $ext_vmx = ".vmx"
    condition:
        uint32(0) == 0x464c457f and  // ELF magic
        all of ($vm_*, $datastore, $ransom_note, $play_marker) and
        2 of ($ext_*)
}
```

**What Surrogate-1 must learn**:
- YARA syntax: `meta`, `strings` (text/hex/regex), `condition`
- Magic byte recognition (PE `MZ` 0x4D5A, ELF 0x7F454C46, Mach-O 0xFEEDFACE)
- PE module imports (`pe.imports("kernel32.dll", "VirtualAlloc")`)
- Hash modules, math.entropy for packed-binary detection
- Performance: avoid lone `$short` strings, prefer combinations with `for any of`

### 2.3 Splunk SPL (still 30%+ market share)

```splunk
index=windows EventCode=4688
| eval cmdline=lower(CommandLine)
| where match(cmdline, "(?i)(mimikatz|sekurlsa::logonpasswords|invoke-mimikatz|dumpcreds)")
| stats count by Computer, User, ParentProcessName, CommandLine
| where count > 0
```

### 2.4 Elastic EQL (sequence-aware)

```eql
sequence by host.id with maxspan=5m
  [ process where process.name == "powershell.exe" and
    process.command_line : ("*Invoke-Mimikatz*", "*sekurlsa::logonpasswords*") ]
  [ network where network.direction == "egress" and
    not destination.ip in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16") ]
```

### 2.5 Microsoft Sentinel KQL

```kql
DeviceProcessEvents
| where Timestamp > ago(7d)
| where ProcessCommandLine has_any ("Invoke-Mimikatz", "sekurlsa::logonpasswords",
    "DumpCreds", "DumpCerts", "crypto::certificates")
| extend ATT_CK_Technique = "T1003"
| project Timestamp, DeviceName, AccountName, ProcessCommandLine, InitiatingProcessFileName
```

### 2.6 Snort/Suricata IDS (network)

```suricata
alert http any any -> any any (msg:"ET TROJAN Possible C2 - Cobalt Strike Beacon";
  flow:established,to_server;
  http.uri; content:"/submit.php"; startswith;
  http.user_agent; pcre:"/^Mozilla\/5\.0 \(compatible; MSIE 10\.0; Windows NT 6\.[12]\)$/";
  classtype:trojan-activity; sid:1000001; rev:1;)
```

### 2.7 Cloud-native detection (AWS GuardDuty, CloudTrail)

GuardDuty consumes CloudTrail mgmt+data events, VPC Flow Logs, DNS logs, EKS audit logs,
S3 data events, Lambda activity, Malware Protection runtime. Maps findings to MITRE
ATT&CK in 2025 update. Common high-value findings:

| Finding | MITRE | Meaning |
|---------|-------|---------|
| `UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B` | T1078.004 | Login from anonymizing proxy |
| `Recon:EC2/PortProbeUnprotectedPort` | T1046 | Scanning open ports |
| `Persistence:IAMUser/CredentialExfiltration` | T1552 | Stolen long-term creds |
| `CredentialAccess:IAMUser/AnomalousBehavior` | T1078 | API anomaly per IAM principal |
| `Trojan:EC2/BlackholeTraffic` | T1071 | Beaconing to known C2 |
| `Exfiltration:S3/AnomalousBehavior` | T1567.002 | Unusual S3 GetObject volume |
| `DefenseEvasion:IAMUser/AnomalousBehavior` | T1562 | DeleteTrail, DisableConfig |

**Detection-as-Code (Panther / Datadog Cloud SIEM)**:

```python
# panther rule - aws root user usage
def rule(event):
    return (
        event.get("eventSource") == "signin.amazonaws.com"
        and event.get("userIdentity", {}).get("type") == "Root"
        and event.get("eventName") in ("ConsoleLogin", "AssumeRoot")
    )

def title(event):
    return f"AWS Root User Login from {event.get('sourceIPAddress')}"

def severity(event):
    return "HIGH"
```

---

## 3. MITRE ATT&CK v18 (October 2025) — the framework

**Stats (Enterprise)**: 14 Tactics, 216 Techniques, 475 Sub-techniques, 172 Groups,
784 Software, 52 Campaigns, 44 Mitigations, **691 Detection Strategies**, **1739 Analytics**,
106 Data Components.

### 3.1 The 14 Enterprise Tactics (TA0001 → TA0043, gaps from deprecation)

1. **Reconnaissance** (TA0043) — TIDs T1589 (gather identity info), T1595 (active scanning), T1592 (gather victim host info), T1591 (org info), T1590 (network info), T1597 (search closed sources), T1596 (open technical DBs), T1593 (open websites), T1594 (search victim-owned websites)
2. **Resource Development** (TA0042) — T1583 (acquire infra), T1584 (compromise infra), T1587 (develop capabilities), T1585 (establish accounts), T1588 (obtain capabilities), T1608 (stage capabilities), T1586 (compromise accounts)
3. **Initial Access** (TA0001) — T1190 (exploit public-facing app), T1133 (external remote services), T1199 (trusted relationship), T1078 (valid accounts), T1566 (phishing), T1091 (replication via removable media), T1195 (supply chain), T1200 (hardware additions), T1189 (drive-by), T1659 (content injection)
4. **Execution** (TA0002) — T1059 (cmd/scripting), T1106 (native API), T1129 (shared modules), T1559 (IPC), T1203 (exploit for client execution), T1610 (deploy container), T1612 (build image on host), T1204 (user execution), T1648 (serverless execution), T1053 (scheduled task/job), T1569 (system services), T1047 (WMI)
5. **Persistence** (TA0003) — T1098 (account manipulation), T1547 (boot/logon autostart), T1037 (boot/logon init scripts), T1543 (create system process), T1136 (create account), T1546 (event triggered), T1546.003 (WMI subscription), T1554 (compromise client software binary), T1525 (implant container image), T1556 (modify auth process), T1574 (hijack execution flow), T1505 (server software component), T1078 (valid accounts), T1505.003 (web shell)
6. **Privilege Escalation** (TA0004) — T1548 (abuse elevation control), T1134 (access token manip), T1037 (boot/logon init scripts), T1543 (create/modify system process), T1484 (domain policy mod), T1611 (escape to host), T1546 (event triggered), T1068 (exploit for priv esc), T1574 (hijack exec flow), T1055 (process injection), T1053 (scheduled task), T1078 (valid accounts)
7. **Defense Evasion** (TA0005) — T1548 (abuse elevation), T1134 (access token), T1197 (BITS jobs), T1140 (deobfuscate), T1006 (direct volume access), T1610 (deploy container), T1612 (build image on host), T1140 (deobfuscate), T1006 (direct volume), T1622 (debugger evasion), T1564 (hide artifacts), T1574 (hijack exec flow), T1562 (impair defenses), T1070 (indicator removal), T1202 (indirect command exec), T1036 (masquerading), T1556 (modify auth process), T1578 (modify cloud compute infra), T1112 (modify registry), T1601 (modify system image), T1599 (network boundary bridging), T1027 (obfuscated files), T1542 (pre-OS boot), T1055 (process injection), T1207 (rogue domain controller), T1014 (rootkit), T1218 (signed binary proxy exec), T1216 (signed script proxy), T1553 (subvert trust controls), T1221 (template injection), T1205 (traffic signaling), T1535 (unused/unsupported cloud regions), T1550 (use alternate auth), T1078 (valid accounts), T1497 (virtualization/sandbox evasion), T1600 (weaken encryption), T1220 (XSL script processing) — **NOTE: Defense Evasion deprecated in v19 (Apr 2026)**
8. **Credential Access** (TA0006) — T1110 (brute force), T1555 (credentials from password stores), T1212 (exploit for credential access), T1187 (forced authentication), T1606 (forge web credentials), T1056 (input capture), T1556 (modify auth), T1111 (multi-factor auth interception), T1621 (MFA request generation), T1040 (network sniffing), T1003 (OS credential dumping), T1528 (steal application access token), T1649 (steal/forge auth certs), T1558 (steal/forge Kerberos), T1539 (steal web session cookie), T1552 (unsecured credentials)
9. **Discovery** (TA0007) — T1087 (account discovery), T1010 (app window discovery), T1217 (browser bookmark), T1580 (cloud infra discovery), T1538 (cloud service dashboard), T1526 (cloud service discovery), T1613 (container/resource discovery), T1622 (debugger evasion), T1652 (device driver discovery), T1482 (domain trust discovery), T1083 (file/directory discovery), T1615 (group policy discovery), T1654 (log enumeration), T1046 (network service scanning), T1135 (network share discovery), T1040 (network sniffing), T1201 (password policy discovery), T1120 (peripheral device discovery), T1069 (permission groups discovery), T1057 (process discovery), T1012 (query registry), T1018 (remote system discovery), T1518 (software discovery), T1082 (system info discovery), T1614 (system location), T1016 (system network config), T1049 (system network connections), T1033 (system owner/user), T1007 (system service), T1124 (system time), T1497 (virt/sandbox evasion)
10. **Lateral Movement** (TA0008) — T1210 (exploit remote services), T1534 (internal spearphishing), T1570 (lateral tool transfer), T1563 (remote service session hijack), T1021 (remote services), T1091 (replication thru removable media), T1072 (software deployment tools), T1080 (taint shared content), T1550 (use alt auth)
11. **Collection** (TA0009) — T1560 (archive collected), T1123 (audio capture), T1119 (auto collection), T1185 (browser session hijack), T1115 (clipboard data), T1530 (cloud storage), T1602 (data from config repo), T1213 (data from info repo), T1005 (data from local), T1039 (data from network shared drive), T1025 (data from removable), T1074 (data staged), T1114 (email collection), T1056 (input capture), T1113 (screen capture), T1125 (video capture)
12. **Command and Control** (TA0011) — T1071 (app layer protocol), T1092 (comm thru removable media), T1659 (content injection), T1132 (data encoding), T1001 (data obfuscation), T1568 (dynamic resolution), T1573 (encrypted channel), T1008 (fallback channels), T1665 (hide infra), T1105 (ingress tool transfer), T1104 (multi-stage channels), T1095 (non-app layer), T1571 (non-standard port), T1572 (protocol tunneling), T1090 (proxy), T1219 (remote access tools), T1205 (traffic signaling), T1102 (web service)
13. **Exfiltration** (TA0010) — T1020 (auto exfil), T1030 (data transfer size limits), T1048 (exfil over alt protocol), T1041 (exfil over C2 channel), T1011 (exfil other network medium), T1052 (exfil physical medium), T1567 (exfil web service), T1029 (scheduled transfer), T1537 (transfer to cloud account)
14. **Impact** (TA0040) — T1531 (account access removal), T1485 (data destruction), T1486 (data encrypted for impact / **ransomware**), T1565 (data manipulation), T1491 (defacement), T1561 (disk wipe), T1499 (endpoint DoS), T1495 (firmware corruption), T1490 (inhibit system recovery), T1498 (network DoS), T1496 (resource hijacking), T1489 (service stop), T1529 (system shutdown/reboot), T1657 (financial theft)

### 3.2 Sub-techniques worth memorizing (highest hit rate in real incidents)

- **T1059.001** PowerShell, **T1059.003** Windows cmd, **T1059.004** Unix shell, **T1059.005** Visual Basic, **T1059.006** Python
- **T1003.001** LSASS Memory, **T1003.002** SAM, **T1003.003** NTDS, **T1003.005** Cached domain creds, **T1003.006** DCSync, **T1003.008** /etc/passwd shadow
- **T1078.001** Default accts, **T1078.002** Domain accts, **T1078.003** Local accts, **T1078.004** Cloud accts
- **T1566.001** Spearphishing attachment, **T1566.002** link, **T1566.003** via service
- **T1547.001** Reg run keys/Startup folder, **T1547.009** Shortcut modification, **T1547.014** Active Setup
- **T1021.001** RDP, **T1021.002** SMB/Admin shares, **T1021.004** SSH, **T1021.005** VNC, **T1021.006** WinRM
- **T1486** ransomware (Impact tactic)

---

## 4. Threat Hunting

### 4.1 Pyramid of Pain (David Bianco, SANS canon)

```
        ┌─────────────────────┐
        │   TTPs (TOUGH!)     │ ← Hunt here for adversary persistence
        ├─────────────────────┤
        │   Tools             │
        ├─────────────────────┤
        │   Network/Host Arts │
        ├─────────────────────┤
        │   Domain Names      │
        ├─────────────────────┤
        │   IP Addresses      │
        ├─────────────────────┤
        │   Hash Values (TRIVIAL) │ ← Most IOC feeds live here
        └─────────────────────┘
```

Lower = trivial to change for attacker. Top = weeks of retooling. Hash blocks = whack-a-mole.
TTP detection (e.g., "any process spawning encoded PowerShell from Office") forces real cost
on the adversary.

### 4.2 Hypothesis-Driven Hunting (4-step cycle)

1. **Form hypothesis** — name a specific behavior, data source, expected indicator.
   Example: *"Adversary uses living-off-the-land binaries (LOLBins) to download C2
   payload via certutil.exe; expect Sysmon EID 1 with cmdline `certutil -urlcache -f`."*
2. **Search for evidence** — query SIEM/EDR for the behavior pattern across the
   defined window. Use cross-source enrichment.
3. **Analyze findings** — distinguish benign IT-admin use vs adversary use. Pivot on
   parent process, user, time, network destination.
4. **Respond + refine** — promote validated hits to detection rules; document the
   hunt in MISP or Confluence; iterate hypothesis.

### 4.3 LOLBins / LOLBAS / LOLDrivers / LOOBINS

LOLBAS (Windows): https://lolbas-project.github.io/ — 200+ documented binaries.
GTFOBins (Linux): https://gtfobins.github.io/. LOLDrivers (vulnerable drivers):
https://www.loldrivers.io/. LOOBINS (macOS): https://www.loobins.io/.

**High-yield LOLBins to hunt**:

| Binary | Abuse | Detection idea |
|--------|-------|----------------|
| `certutil.exe` | Download, encode/decode | `certutil -urlcache -f http://...` |
| `bitsadmin.exe` | Background download | `bitsadmin /transfer` w/ http URL |
| `mshta.exe` | Run HTA / JS | `mshta http*` or with `vbscript:` |
| `regsvr32.exe` | Squiblydoo proxy exec | `regsvr32 /s /i:http*` |
| `rundll32.exe` | DLL exec, JS exec | `rundll32 javascript:` |
| `installutil.exe` | Trusted .NET exec | with `/u /logfile=` and uncommon path |
| `msbuild.exe` | Inline tasks compile-and-run | XML w/ `<UsingTask>` |
| `wmic.exe` | Remote exec, info disclosure | `wmic /node:` external host |
| `powershell.exe` | Everything | encoded `-enc`, `-w hidden`, `IEX (New-Object Net.WebClient).Downloadstring` |

**GTFOBins to hunt (Linux)**:

| Binary | Abuse | Detection |
|--------|-------|-----------|
| `find` | sudo | `find . -exec /bin/sh \;` w/ sudo context |
| `awk` | sudo shell | `awk 'BEGIN {system("/bin/sh")}'` |
| `nmap` | --interactive | older nmap with `--interactive` flag |
| `vim` | :!sh | `vim -c '!/bin/sh'` |
| `tar` | checkpoint actions | `--checkpoint=1 --checkpoint-action=exec=` |

### 4.4 Adversary Emulation Datasets (TRAINING DATA for Surrogate-1)

- **Atomic Red Team** (https://github.com/redcanaryco/atomic-red-team) — 1500+
  per-technique atomic tests with PowerShell/bash/CMD payloads + cleanup
- **MITRE Caldera** (https://github.com/mitre/caldera) — automated adversary
  emulation; can generate scripted attack chains aligned to ATT&CK
- **Stratus Red Team** (https://stratus-red-team.cloud/) — cloud-native (AWS, Azure, GCP, K8s)
- **APTSimulator** — Windows endpoint adversary noise
- **Sliver** (BishopFox), **Mythic** (Mythic-C2), **Cobalt Strike** (commercial) — C2 frameworks
- **Splunk Attack Range / BoTS dataset** — labeled SOC training data
- **CyberDefenders.org**, **TryHackMe**, **HackTheBox** — challenge datasets w/ writeups

### 4.5 IOC Types + Sharing

| Type | Format | Sharing | Stability |
|------|--------|---------|-----------|
| File hash | MD5/SHA1/SHA256 | STIX, MISP, OTX | days–weeks |
| IP address | IPv4/v6 + CIDR | STIX/TAXII | hours–weeks |
| Domain | FQDN | STIX, MISP | hours–months |
| URL | full URL + path | STIX | minutes–days |
| Email | sender + subject + attach hash | MISP | days |
| Mutex | string | YARA | months |
| Reg key | path | Sigma | months–years |
| Cert thumbprint | SHA1 | OTX | months |
| TTP | ATT&CK TID | STIX 2.1, MISP galaxy | years |

---

## 5. Incident Response Playbooks

### 5.1 Frameworks

- **NIST SP 800-61 Rev. 3** (April 2025, supersedes Rev. 2) — restructured around NIST CSF 2.0
  Functions (Govern, Identify, Protect, Detect, Respond, Recover). Now reads like a best
  practices guide for management. URL: csrc.nist.gov/pubs/sp/800/61/r3/final
- **SANS PICERL** — 6 phases for practitioners:
  1. **Preparation** — runbooks, tooling, comms tree, on-call rotation, tabletop cadence
  2. **Identification** — alert validation, scoping
  3. **Containment** — short-term (isolate host) → long-term (image, forensic clone)
  4. **Eradication** — remove malware, close access vector, patch root cause
  5. **Recovery** — rebuild, monitor for re-infection, restore service
  6. **Lessons Learned** — post-mortem, detection gap closure, runbook update
- **NIST CSF 2.0** Functions: Govern (new), Identify, Protect, Detect, Respond, Recover

Use SANS PICERL for **operational execution**, NIST 800-61 r3 for **management /
compliance alignment**. Most teams pair both.

### 5.2 Ransomware Playbook (NIST/SANS aligned, 2025 baseline)

```markdown
# Ransomware IR Playbook — vNIST-800-61-r3 / SANS-PICERL hybrid

## DETECT (CSF: DE.AE-01, DE.CM-01, DE.CM-07)
- [ ] EDR alert: mass file modification + extension change pattern (`.lock`, `.crypt`, `.<actor>`)
- [ ] SIEM rule: T1486 detection — high entropy writes from a single process
- [ ] User report: ransom note observed (`README*.txt`, `HOW_TO_DECRYPT*`)

## IDENTIFY / TRIAGE (CSF: ID.AM-01, ID.AM-02)
- [ ] Confirm scope: how many hosts, which file shares, which cloud volumes (S3, EBS, RDS)
- [ ] Identify ransomware family (ID Ransomware, NoMoreRansom, EDR signature)
- [ ] Determine encryption variant (online key vs offline; symmetric AES-256 + RSA-2048 wrapping is the norm)

## CONTAIN (CSF: RS.MI-01, RS.MI-02)
- [ ] **Network isolation** — EDR-initiated host quarantine, VLAN block, AWS security group → 0.0.0.0/0 deny
- [ ] Disable affected user accounts (do NOT change password yet — preserve forensic context)
- [ ] Pull memory image BEFORE shutdown (Volatility-compatible — `winpmem` / `LiME`)
- [ ] Snapshot disks (EBS snapshot, VMware snapshot) — chain-of-custody log
- [ ] Block C2 indicators at firewall + DNS sinkhole

## ERADICATE (CSF: RS.MI-03)
- [ ] Identify root cause: phishing? RDP brute-force? unpatched VPN (CVE)? supply-chain?
- [ ] Patch the entry vector across the fleet
- [ ] Rotate ALL credentials touched by infected hosts (KRBTGT 2x for AD; service accts; cloud keys)
- [ ] Hunt for persistence: scheduled tasks, services, registry run keys, Active Setup, WMI subscriptions
- [ ] Validate AD: ACL changes, new admin accts, Golden Ticket evidence

## RECOVER (CSF: RC.RP-01)
- [ ] **DO NOT pay** — FBI / CISA guidance; legal review under OFAC sanctions list
- [ ] Restore from immutable backup (verify backup is clean — ransomware often dwells 30–60 days)
- [ ] Validate restored services + monitor 14 days for re-infection
- [ ] Rebuild AD if domain controllers compromised (DSRM password rotation, KRBTGT rotation 2x w/ 24h gap)

## LESSONS LEARNED (CSF: RC.IM-01, RC.IM-02)
- [ ] Post-mortem within 5 business days
- [ ] Detection-gap analysis → new Sigma/EDR rules
- [ ] Tabletop within 30 days to validate playbook update
- [ ] Update SBOM / patching SLAs based on root cause

## NOTIFICATION (regulatory)
- [ ] GDPR Art. 33 — 72h to supervisory authority if PII affected
- [ ] HIPAA Breach Notification Rule — 60d to individuals + HHS
- [ ] PCI-DSS — immediate to acquirer + brands if cardholder data
- [ ] SEC Regulation S-K Item 1.05 — 4 business days for material incidents (US public companies)
- [ ] CISA voluntary report; FBI IC3 if extortion
```

### 5.3 Other playbook templates needed

- **Account compromise / BEC** — disable + force MFA reset + audit OAuth grants + audit forward rules
- **Data exfiltration** — DLP forensic preservation + outbound traffic analysis + legal review
- **Insider threat** — HR + legal partnership; monitor without alerting suspect
- **Supply chain (SolarWinds-style)** — full SBOM diff against last-known-good; rotate all secrets ever exposed to compromised tooling; assume worst-case privilege held
- **Zero-day exploitation** — emergency change board; virtual patching via WAF/IPS; vendor coordination

### 5.4 Tabletop exercises

- CISA Tabletop Exercise Packages (CTEP)
- MITRE ATT&CK Evaluations (round 6 = Enterprise 2024, round 7 = ICS 2025)
- SANS internal-only "Cyber Defense Forensics Tabletop"
- Cadence: at least quarterly for senior IR; annual for executive/legal

---

## 6. Vulnerability Management

### 6.1 CVE / CVSS / EPSS / KEV / VEX

- **CVE** (Common Vulnerabilities and Exposures) — MITRE/CISA registry of vulns, ID format
  `CVE-YYYY-NNNNN`. ~30k+ CVEs published in 2024.
- **CVSS** (Common Vulnerability Scoring System) — 0.0–10.0 severity. **CVSS 4.0** (Nov 2023)
  fixed v3 weaknesses with explicit threat metrics + supplemental metrics. Most orgs still on
  v3.1 transitioning. CISA recommends prioritizing **CVSS ≥ 7.0** AND **KEV listed** AND
  **EPSS ≥ 0.5**.
- **EPSS** (Exploit Prediction Scoring System) — FIRST.org daily-updated probability that a
  CVE will be exploited in next 30 days. 0.0–1.0. Top 5% covers 95% of real exploitation.
- **KEV** (Known Exploited Vulnerabilities) — CISA-maintained list of CVEs **observed in
  active exploitation**. ~1100 CVEs as of 2026. Federal agencies must patch on KEV cadence.
- **VEX** (Vulnerability Exploitability eXchange) — machine-readable statement from vendor
  saying *"yes our product uses vulnerable lib X, but in our usage it's not exploitable
  because Y"*. Reduces SBOM noise. Formats: CycloneDX VEX, OpenVEX, CSAF VEX.

### 6.2 SBOM standards

- **CycloneDX** (OWASP) — full-stack: SBOM, SaaSBOM, HBOM, OBOM, VDR, VEX. JSON or XML.
- **SPDX** (Linux Foundation, ISO/IEC 5962) — older, license-focused.
- **CERT-In SBOM Guidelines 2025** require both formats from suppliers + VEX.

**SBOM tools**: `syft` (Anchore), `cyclonedx-bom`, `cdxgen`, `trivy sbom`, GitHub
dependency-graph + dependency-submission-action.

### 6.3 Scanners (cheatsheet for Surrogate-1)

| Tool | Type | Targets | License | Surrogate command idiom |
|------|------|---------|---------|------------------------|
| **Trivy** (Aqua) | All-in-one | Container, FS, Git, IaC, K8s, secrets, license | Apache-2.0 | `trivy image --severity HIGH,CRITICAL --format json img:tag` |
| **Grype** (Anchore) | SCA | Containers, FS, SBOMs | Apache-2.0 | `grype dir:./ -o json` |
| **Snyk** | SCA + SAST + IaC | Multi | Commercial (free OSS) | `snyk test`, `snyk container test`, `snyk iac test` |
| **Dependabot** (GitHub) | SCA | Repos | Free | enabled via `.github/dependabot.yml` |
| **Renovate** (Mend) | SCA + auto-PR | Repos | BSD | `renovate.json` |
| **OSV-Scanner** (Google) | SCA | OSV-format | Apache-2.0 | `osv-scanner --recursive .` |
| **Nessus** (Tenable) | Network | Hosts, network | Commercial | n/a |
| **OpenVAS** / **Greenbone** | Network | Hosts | GPL | `gvm-cli` |
| **Qualys VMDR** | Network + cloud | Hosts, cloud | Commercial | n/a |
| **Wiz** / **Orca** / **Prisma** | CSPM + CWPP + Vuln | Cloud | Commercial | n/a |

### 6.4 Patching SLA matrix (industry baseline)

| Severity | Internet-facing | Internal | Cloud workload |
|----------|----------------|----------|---------------|
| Critical (CVSS ≥9 + KEV) | 24h | 48h | 24h |
| High (CVSS 7–8.9) | 7d | 14d | 7d |
| Medium (4–6.9) | 30d | 60d | 30d |
| Low (<4) | 90d | next maintenance | 90d |

Compliance overrides: PCI-DSS req 6.3.3 = "critical patches within 1 month"; HIPAA Security
Rule §164.308 = reasonable + risk-based; FedRAMP = monthly POA&M update.

---

## 7. Cloud Security (CSPM / CWPP / CIEM / CNAPP)

### 7.1 Categories

- **CSPM** (Cloud Security Posture Management) — config drift, compliance baseline,
  misconfig detection. *99% of cloud breaches in 2025 traced to misconfig.*
- **CWPP** (Cloud Workload Protection Platform) — runtime protection on VMs/containers/serverless.
- **CIEM** (Cloud Infrastructure Entitlement Management) — IAM least-privilege analysis.
- **CNAPP** (Cloud-Native Application Protection Platform) — CSPM + CWPP + CIEM merged.
- **DSPM** (Data Security Posture Management) — sensitive-data discovery + classification.

### 7.2 Vendors + Open Source

| Tier | Tool | Type | Note |
|------|------|------|------|
| Big 3 CSPM | **Wiz** | Agentless, Security Graph | 15-min deploy, market leader |
| Big 3 CSPM | **Orca Security** | Agentless | SideScanning patent |
| Big 3 CSPM | **Prisma Cloud** (Palo Alto) | Full CNAPP | code-to-cloud |
| CSPM | **Aqua Security** | CNAPP w/ CI/CD | Trivy maintainer |
| CSPM | **Lacework** (now Fortinet) | ML-driven | Polygraph |
| Open Source | **Prowler** | AWS/Azure/GCP/K8s/M365 | 700+ checks, AWS Native (used in `cdk-infrastructure/`) |
| Open Source | **ScoutSuite** (NCC Group) | Multi-cloud | Python |
| Open Source | **CloudSploit** (Aqua) | Multi-cloud | Node.js |
| Open Source | **Checkov** (Bridgecrew/PA) | IaC + cloud | Python; CDK + TF |
| Open Source | **Steampipe** (Turbot) | SQL-over-cloud | Powerful for ad-hoc audits |
| Open Source | **CloudQuery** | Cloud-to-DB | Postgres queries |
| Open Source | **PMapper** (NCC Group) | AWS IAM graph | privesc paths |

### 7.3 AWS native security stack

```
Detection:
  Amazon GuardDuty           — threat detection (CloudTrail, VPC Flow, DNS, EKS)
  Amazon Inspector           — vuln assessment (EC2, ECR, Lambda)
  AWS IAM Access Analyzer    — external/unused access analysis
  AWS Config                 — config compliance + drift
  AWS CloudTrail Lake        — audit query

Posture:
  AWS Security Hub           — aggregator of all of the above + custom
  AWS Audit Manager          — automated evidence for SOC2/ISO/PCI/HIPAA/NIST
  AWS Trusted Advisor        — best-practice baseline

Identity:
  AWS IAM Identity Center (SSO)
  AWS IAM Roles Anywhere     — non-AWS workloads → AWS w/o keys
  AWS Verified Permissions   — Cedar policy engine

Data:
  AWS Macie                  — sensitive data discovery (S3)
  AWS KMS / CloudHSM         — key mgmt
  AWS Secrets Manager        — secret rotation

Network:
  AWS Network Firewall       — Suricata-compatible
  AWS WAF + Shield Advanced  — L7 + DDoS
  VPC Flow Logs + Route 53 query logs

Response:
  AWS Systems Manager Incident Manager
  Amazon Detective           — investigation graph
```

### 7.4 GCP equivalents

Google Security Command Center (SCC) Premium/Enterprise = GuardDuty + Inspector + Hub
combined; Chronicle = SIEM; Mandiant = IR.

### 7.5 Azure equivalents

Microsoft Defender for Cloud = CSPM + CWPP; Microsoft Sentinel = SIEM/SOAR; Microsoft
Defender XDR = endpoint+identity+email+cloud apps.

---

## 8. Container + Kubernetes Security

### 8.1 The 4 Cs (Cloud Native Security model)

1. **Cloud** — AWS/GCP/Azure account hardening
2. **Cluster** — control plane + node + RBAC
3. **Container** — image + runtime
4. **Code** — app vulns

### 8.2 Image scanning

- **Trivy** — Aqua, Apache-2.0. `trivy image --severity HIGH,CRITICAL alpine:3.18`
- **Grype** — Anchore, Apache-2.0. SBOM-driven via syft.
- **Snyk Container** — commercial.
- **Clair** — Quay.io's scanner.
- **Docker Scout** — Docker Inc.

### 8.3 Runtime detection

**Falco** (CNCF) — uses eBPF to monitor Linux syscalls. Custom rules in YAML.

```yaml
- rule: Shell spawned in container
  desc: A shell was spawned in a container
  condition: >
    container and shell_procs and proc.tty != 0 and container_entrypoint
  output: >
    Shell spawned in a container (user=%user.name container=%container.id
    container_image=%container.image.repository shell=%proc.name parent=%proc.pname)
  priority: WARNING
  tags: [container, shell, mitre_execution]

- rule: Write below /etc
  desc: An attempt to write to any file below /etc
  condition: >
    write_etc_common
  output: >
    File below /etc opened for writing (user=%user.name command=%proc.cmdline
    parent=%proc.pname pcmdline=%proc.pcmdline file=%fd.name program=%proc.name)
  priority: ERROR
  tags: [filesystem, mitre_persistence]
```

**Sysdig Secure** — commercial Falco++.

### 8.4 Admission control (policy-as-code)

**Kyverno** (CNCF) — Kubernetes-native YAML policies, no Rego required.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-privileged-containers
spec:
  validationFailureAction: Enforce
  rules:
    - name: privileged-containers
      match:
        any:
          - resources:
              kinds: [Pod]
      validate:
        message: "Privileged mode is disallowed"
        pattern:
          spec:
            =(securityContext):
              =(privileged): "false"
            containers:
              - name: "*"
                =(securityContext):
                  =(privileged): "false"
```

**OPA Gatekeeper** — Rego-based admission control.
**Pod Security Standards** (replaces deprecated PSP, Kubernetes ≥1.25):
- `privileged` (no restrictions, not for prod)
- `baseline` (minimally restrictive, prevents known privilege escalations)
- `restricted` (heavily restricted, recommended for prod) — apply via namespace label:
  `pod-security.kubernetes.io/enforce: restricted`

**Kubescape** (ARMO, CNCF) — multi-framework K8s security scanner (NSA-CISA, MITRE,
CIS, Pod Security Standards).
**KubeArmor** (AccuKnox, CNCF) — runtime attack detection w/ Linux LSMs (BPF-LSM, AppArmor, SELinux).

### 8.5 Service mesh security (Istio mTLS)

```yaml
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT
```

---

## 9. Secure Code Review

### 9.1 SAST (Static Application Security Testing)

- **Semgrep** (semgrep.dev) — open-source SAST, rules in YAML, fast (~30s for 100k LOC),
  AI-assisted in 2025. Rules at semgrep.dev/r. Languages: 30+. Used by GitLab, Snowflake, Snowflake.
- **CodeQL** (GitHub Advanced Security) — taint analysis via Datalog queries. Best Java/JS/Py/Go/C.
  Free for public repos.
- **SonarQube** / **SonarCloud** — code quality + security rules.
- **Snyk Code** — commercial AI-driven SAST.
- **Checkmarx**, **Veracode**, **Fortify** — enterprise/legacy.

**Semgrep rule example**:

```yaml
rules:
  - id: hardcoded-aws-key
    message: Hardcoded AWS access key detected
    pattern-regex: 'AKIA[0-9A-Z]{16}'
    languages: [generic]
    severity: ERROR
    metadata:
      cwe: 'CWE-798: Use of Hard-coded Credentials'
      owasp: 'A07:2021 - Identification and Authentication Failures'
      category: security
```

### 9.2 DAST

- **OWASP ZAP** (free) — full active+passive web scanner.
- **Burp Suite Pro** (PortSwigger) — manual + automated.
- **Acunetix**, **Netsparker (Invicti)**, **Tenable Web App Scanning** — commercial.
- **Nuclei** (ProjectDiscovery) — template-based fast scanner.

### 9.3 SCA + Dep Track

- **Trivy**, **Grype**, **OSV-Scanner**, **Snyk**, **Dependabot**, **Renovate**, **OWASP Dependency-Check**
- **Dependency-Track** (OWASP) — SBOM platform, ingest CycloneDX, alert on new CVEs.

### 9.4 IaC Scanning

| Tool | IaC | Cloud |
|------|-----|-------|
| **Checkov** (Bridgecrew/Palo Alto) | Terraform, CFN, K8s, Helm, ARM, Bicep, Serverless, Dockerfile | All |
| **tfsec** (Aqua, now in Trivy) | Terraform | All |
| **KICS** (Checkmarx) | Terraform, K8s, CFN, Ansible, Helm, Dockerfile | All |
| **cfn-lint** (AWS) | CFN | AWS |
| **cfn-nag** (Stelligent) | CFN | AWS |
| **cfn-guard** (AWS) | CFN | AWS |
| **Terrascan** (Tenable) | Terraform, K8s, Helm | All |
| **regula** (Fugue) | Terraform, CFN | All |
| **kube-score** | K8s | n/a |
| **Polaris** (Fairwinds) | K8s | n/a |

`cfn-guard` rule example (AWS native, used in user's `cdk-infrastructure/`):

```hcl
rule s3_bucket_public_access_block {
  Resources.*[ Type == 'AWS::S3::Bucket' ] {
    Properties {
      PublicAccessBlockConfiguration exists
      PublicAccessBlockConfiguration {
        BlockPublicAcls == true
        BlockPublicPolicy == true
        IgnorePublicAcls == true
        RestrictPublicBuckets == true
      }
    }
  }
}
```

### 9.5 Secret scanning

| Tool | Note |
|------|------|
| **TruffleHog** | Verifies secrets are LIVE (not just regex match) |
| **GitLeaks** | Fast, regex-based, pre-commit hook friendly |
| **detect-secrets** (Yelp) | Plugin architecture |
| **GitHub Secret Scanning** | Native, partner notifications (AWS, GCP, etc. revoke on detection) |
| **AWS git-secrets** | AWS-specific |

### 9.6 PII detection / classification

- **Microsoft Presidio** — open-source PII detection + redaction.
- **AWS Macie** — S3-only.
- **GCP DLP** / **Cloud Data Loss Prevention API**.
- **Privacera**, **BigID**, **OneTrust** — commercial DSPM.

### 9.7 License compliance

- **FOSSology** (open source).
- **Black Duck** (Synopsys).
- **WhiteSource** / **Mend.io**.
- **scancode-toolkit** + **dependency-track**.

---

## 10. Compliance Frameworks (depth on each)

### 10.1 SOC 2 Type II

AICPA SSAE-18. **5 Trust Service Criteria (TSC)**:
1. **Security** (mandatory)
2. **Availability**
3. **Processing Integrity**
4. **Confidentiality**
5. **Privacy**

**Common Criteria (CC1–CC9)** — basis of every SOC 2 report:
- CC1: Control Environment (governance, integrity, ethics)
- CC2: Communication & Information
- CC3: Risk Assessment
- CC4: Monitoring Activities
- CC5: Control Activities
- CC6: Logical & Physical Access Controls (auth, MFA, access reviews, encryption)
- CC7: System Operations (change mgmt, incident mgmt, monitoring)
- CC8: Change Management (CI/CD, SDLC)
- CC9: Risk Mitigation (vendor mgmt, incident response, BCP/DR)

**Type I** = point-in-time. **Type II** = operational effectiveness over 3–12 months
(typical = 6 or 12 months).

### 10.2 ISO 27001:2022 + Annex A controls

ISO/IEC 27001:2022 (replacing 2013 version) — 93 Annex A controls in 4 themes:
- **A.5 Organizational** (37 controls)
- **A.6 People** (8 controls)
- **A.7 Physical** (14 controls)
- **A.8 Technological** (34 controls)

**Mapping**: 80–100% of SOC 2 controls map to ISO 27001 Annex A. ISO 27002:2022 provides
implementation guidance for each control.

### 10.3 GDPR (EU 2016/679)

- **Lawful basis** (Art. 6) — consent, contract, legal obligation, vital interests, public task, legitimate interests
- **Data subject rights** (Art. 15–22): access, rectification, **erasure ("right to be forgotten")**, restriction, portability, objection, no automated decision
- **DPIA** (Art. 35) — Data Protection Impact Assessment for high-risk processing
- **Breach notification** (Art. 33) — 72h to supervisory authority
- **DPO** (Art. 37) — required for public authority, large-scale monitoring, sensitive data
- **Records of Processing Activities** (Art. 30)
- **Cross-border transfers** (Ch. V) — SCCs, BCRs, adequacy decisions

### 10.4 HIPAA (US 1996, OCR enforcement)

- **Privacy Rule** (PHI use/disclosure)
- **Security Rule** — 3 safeguards:
  - **Administrative** (164.308) — risk analysis, workforce training, incident procedures
  - **Physical** (164.310) — facility access, device controls
  - **Technical** (164.312) — access control, audit controls, integrity, transmission security, encryption (addressable)
- **Breach Notification Rule** (164.400–414) — 60d to individuals + HHS, media if >500 records
- **HITECH Act** (2009) — extended HIPAA to business associates, increased penalties

### 10.5 PCI-DSS v4.0.1 (March 2024)

12 requirements organized in 6 goals:
1. **Build & maintain secure network** — firewalls (R1), default passwords (R2)
2. **Protect cardholder data** — stored CHD (R3), transit (R4)
3. **Maintain vuln mgmt** — antimalware (R5), develop secure systems (R6)
4. **Implement strong access control** — restrict access (R7), authenticate access (R8), restrict physical (R9)
5. **Monitor & test networks** — track access (R10), test security (R11)
6. **Maintain InfoSec policy** — R12

**Key 2024 changes**: MFA for all CHD access (8.4), stronger password (8.3), automated audit log review (10.4), targeted risk analysis (12.3).
**Network segmentation** required to reduce CDE scope.

### 10.6 NIST CSF 2.0 (Feb 2024)

6 Functions (was 5):
1. **Govern (NEW)** — strategy, risk, policy, supply chain
2. **Identify** — assets, risk, supply chain
3. **Protect** — access control, training, data security
4. **Detect** — anomalies, continuous monitoring
5. **Respond** — incident management, communications, mitigation
6. **Recover** — restoration, communications

108 Subcategories (e.g., GV.OC-01, ID.AM-02, PR.AC-01, DE.CM-01, RS.MA-01, RC.RP-01).
ATT&CK v18 Detection Strategies map directly to DE.CM and DE.AE subcategories.

### 10.7 NIST 800-53 Rev. 5

20 control families: AC (Access Control), AT (Awareness Training), AU (Audit & Acct),
CA (Assessment), CM (Configuration Mgmt), CP (Contingency), IA (Identification & Auth),
IR (Incident Response), MA (Maintenance), MP (Media Protection), PE (Physical),
PL (Planning), PM (Program Mgmt), PS (Personnel), PT (PII), RA (Risk), SA (System&Services),
SC (System Comm), SI (System & Info Integrity), SR (Supply Chain Risk).

Used as basis for FedRAMP (Low/Moderate/High/JAB-High).

### 10.8 CIS Controls v8 / v8.1

**18 Top-Level Controls** (was 20 in v7), each with Safeguards (153 total in v8.1):

1. Inventory and Control of Enterprise Assets
2. Inventory and Control of Software Assets
3. Data Protection
4. Secure Configuration of Enterprise Assets and Software
5. Account Management
6. Access Control Management
7. Continuous Vulnerability Management
8. Audit Log Management
9. Email and Web Browser Protections
10. Malware Defenses
11. Data Recovery
12. Network Infrastructure Management
13. Network Monitoring and Defense
14. Security Awareness and Skills Training
15. Service Provider Management
16. Application Software Security
17. Incident Response Management
18. Penetration Testing

**Implementation Groups**: IG1 (basic), IG2 (mid-size), IG3 (mature). v8.1 adds Governance
function aligned to CSF 2.0.

### 10.9 Compliance crosswalk (key for Surrogate-1 training)

| Area | SOC2 | ISO27001 | NIST 800-53 | PCI-DSS | HIPAA | CSF 2.0 |
|------|------|----------|-------------|---------|-------|---------|
| MFA | CC6.1 | A.8.5 | IA-2 | 8.4 | 164.312(d) | PR.AA-03 |
| Encryption at rest | CC6.7 | A.8.24 | SC-28 | 3.5.1 | 164.312(a)(2)(iv) | PR.DS-01 |
| Encryption in transit | CC6.7 | A.8.24 | SC-8 | 4.2 | 164.312(e)(1) | PR.DS-02 |
| Vuln scanning | CC7.1 | A.8.8 | RA-5 | 11.3 | 164.308(a)(1)(ii)(A) | ID.RA-01 |
| Logging | CC7.2 | A.8.15 | AU-2 | 10.2 | 164.312(b) | DE.CM-01 |
| IR plan | CC7.3 | A.5.24 | IR-4 | 12.10 | 164.308(a)(6) | RS.MA-01 |
| Access review | CC6.2 | A.5.18 | AC-2(j) | 7.2 | 164.308(a)(4) | PR.AA-05 |
| Backup | A1.2 | A.8.13 | CP-9 | (n/a) | 164.308(a)(7)(ii)(A) | RC.RP-01 |

Tools that automate evidence collection: **Vanta**, **Drata**, **Secureframe**, **Tugboat**,
**Sprinto**, **Hyperproof**, **OneTrust GRC**.

---

## 11. DFIR — Digital Forensics & Incident Response

### 11.1 Memory forensics

- **Volatility 3** (volatilityfoundation.org) — Python framework for memory analysis.
  Major rewrite from Volatility 2.6. Plugin-based.
- **Rekall** — Google's fork (less active 2023+).
- **WinDbg** + **!analyze -v** for kernel crash dumps.

**Volatility 3 essential plugins**:

```bash
# Identify OS profile (auto in v3)
vol -f memory.raw windows.info

# Process listing
vol -f memory.raw windows.pslist
vol -f memory.raw windows.pstree
vol -f memory.raw windows.psscan          # find hidden via pool scan

# Network connections
vol -f memory.raw windows.netscan

# Loaded DLLs / modules
vol -f memory.raw windows.dlllist
vol -f memory.raw windows.modules
vol -f memory.raw windows.modscan         # find unlinked

# Code injection
vol -f memory.raw windows.malfind
vol -f memory.raw windows.hollowfind

# Credentials
vol -f memory.raw windows.hashdump
vol -f memory.raw windows.lsadump
vol -f memory.raw windows.cachedump

# Registry (loaded into memory)
vol -f memory.raw windows.registry.hivelist
vol -f memory.raw windows.registry.printkey --key 'Software\Microsoft\Windows\CurrentVersion\Run'

# Linux profiles (via symbol table)
vol -f memory.lime linux.bash             # bash history
vol -f memory.lime linux.pslist
vol -f memory.lime linux.malfind
```

### 11.2 Memory acquisition

- **WinPmem** (Velociraptor) — Windows .raw / .aff4
- **DumpIt** (Magnet) — Windows
- **LiME** (Linux Memory Extractor) — Linux kernel module
- **AVML** (Microsoft) — Linux acquisition without compile-on-target
- **MargaritaShotgun** — remote acquisition over SSH
- **F-Response** / **Belkasoft RAM** — commercial enterprise

### 11.3 Disk forensics

- **dd** / **dcfldd** / **dc3dd** — bit-for-bit imaging
- **FTK Imager** — free Windows imager (E01, AFF, raw)
- **EnCase** — commercial premier suite
- **Autopsy** + **The Sleuth Kit** — open-source disk forensics (Brian Carrier)
- **X-Ways Forensics** — commercial, fast
- **KAPE** (Eric Zimmerman) — targeted artifact collection (~MB not GB)

### 11.4 Timeline reconstruction

- **Plaso/log2timeline** — super timeline tool (l2t_csv, ELK ingestion)
- **Timesketch** (Google) — collaborative timeline analysis
- **MFT** (Master File Table) parsing — `MFTECmd` (Eric Zimmerman)
- **Event log** parsing — `EvtxECmd`, `Hayabusa`
- **Browser history** — `BrowsingHistoryView` (NirSoft), `Hindsight`

### 11.5 Cloud forensics (AWS-focused)

- **Acquisition**: EBS snapshot (preserves state) → mount in forensic VPC → image with dd
- **Logs**: CloudTrail (90d default → S3 forever), VPC Flow Logs, Route 53 query logs,
  EKS audit, Lambda Insights, RDS Performance Insights
- **Tools**:
  - **aws_ir** (ThreatResponse) — AWS-specific IR toolkit
  - **AWS IR Runbooks** (github.com/aws-samples/aws-incident-response-runbooks)
  - **CloudTrail Lake** — SQL queries on years of CloudTrail
  - **Amazon Detective** — pre-built investigation graph
  - **Sumo Logic Cloud SIEM**, **Datadog Cloud SIEM**, **Panther** for cloud-native SIEM

### 11.6 Chain of custody

Required fields per evidence item:
- Collector identity + signature
- Date/time of collection (ISO 8601 + timezone)
- Location (physical / logical)
- Hash (SHA-256 minimum, MD5 + SHA-256 for legacy compat)
- Hardware/software used to collect
- Tamper-evident storage (write-blocker, evidence bag, encrypted container)
- Each transfer logged: from → to → date/time → reason → hash verified

### 11.7 Anti-forensics indicators

- Disabled Windows event log (`wevtutil cl`, `Clear-EventLog`)
- Sysmon stopped or filter altered
- Timestomp via PowerShell `Set-ItemProperty` on `LastWriteTime`
- Wiped MFT $LogFile / NTFS journal
- Memory-only payloads (no disk artifact, MITRE T1620 reflective code loading)
- Shellcode in alternate data streams
- Staging in `Recycle.Bin`, `System Volume Information`, `Temp`

---

## 12. Red Team / Offensive Security (defensive understanding only)

### 12.1 Methodologies

- **PTES** (Penetration Testing Execution Standard) — pre-engagement, intel gathering,
  threat modeling, vuln analysis, exploitation, post-exploitation, reporting
- **NIST 800-115** — Technical Guide to Information Security Testing
- **OWASP Testing Guide v5** — web application testing
- **OWASP API Security Top 10 (2023)** + **2025 update**

### 12.2 OWASP Top 10:2025 (web)

A01: Broken Access Control
A02: Cryptographic Failures
A03: Injection
A04: Insecure Design
A05: Security Misconfiguration
A06: Vulnerable & Outdated Components
A07: Identification & Authentication Failures
A08: Software & Data Integrity Failures (SBOM, supply-chain)
A09: Security Logging & Monitoring Failures
A10: Server-Side Request Forgery (SSRF)

### 12.3 OWASP API Security Top 10 (2023)

API1: Broken Object Level Authorization (BOLA)
API2: Broken Authentication
API3: Broken Object Property Level Authorization
API4: Unrestricted Resource Consumption
API5: Broken Function Level Authorization
API6: Unrestricted Access to Sensitive Business Flows
API7: Server Side Request Forgery
API8: Security Misconfiguration
API9: Improper Inventory Management
API10: Unsafe Consumption of APIs

### 12.4 OWASP Top 10 for LLMs:2025

LLM01: Prompt Injection
LLM02: Sensitive Information Disclosure
LLM03: Supply Chain
LLM04: Data and Model Poisoning
LLM05: Improper Output Handling
LLM06: Excessive Agency
LLM07: System Prompt Leakage
LLM08: Vector and Embedding Weaknesses
LLM09: Misinformation
LLM10: Unbounded Consumption

### 12.5 AD attack chains

- **Kerberoasting** — request TGS for SPN-bound service account, crack offline
- **AS-REP Roasting** — DontReqPreAuth accounts get AS-REP, crack offline
- **DCSync** — replicate AD via MS-DRSR (admin-equivalent right)
- **Golden Ticket** — forge TGT with KRBTGT hash → access anything as anyone
- **Silver Ticket** — forge TGS for specific service
- **Pass-the-Hash** — NTLM hash reuse
- **Pass-the-Ticket** — TGT/TGS reuse
- **Constrained / Unconstrained Delegation** abuse → S4U2Self/Proxy
- **Resource-Based Constrained Delegation** (RBCD) — local computer write right → impersonate

**Tools**: BloodHound (graph relationships), SharpHound (collector), PowerView,
Mimikatz (`sekurlsa`, `lsadump`, `kerberos`), Rubeus (Kerberos abuse), Impacket
(secretsdump, GetUserSPNs, GetNPUsers, smbexec, wmiexec, dcomexec).

### 12.6 Cloud attack chains

- **AWS IAM exfil** — leaked AKIA → assume role chain → cross-account → exfil S3
- **Stratus Red Team** — `stratus detonate aws.execution.ec2-instance-credentials`
  emulates IMDSv1 SSRF + cred theft
- **AWS pacu** (Rhino Security) — AWS-specific exploitation framework
- **CloudGoat** (Rhino) — CTF-style AWS scenarios for training

### 12.7 C2 frameworks (defensive context)

- **Cobalt Strike** (commercial) — most-abused, ~500+ tracked teamserver IOCs
- **Sliver** (BishopFox, OSS) — Go-based, gaining adoption with criminals
- **Mythic** (Mythic-C2, OSS) — modular, multi-language
- **Empire** (BC-Security) — PowerShell + Python
- **Brute Ratel** (commercial, leaked)
- **Havoc** (OSS) — modern post-CS

Detection: JA3/JA3S, JARM TLS fingerprints; named pipes patterns; sleep+jitter beacons;
DNS C2 over TXT/CNAME; HTTPS C2 over Cloudflare/Azure/AWS CDN domain fronting (now blocked).

### 12.8 Reverse engineering

- **Ghidra** (NSA, free) — disassembler + decompiler
- **IDA Pro / IDA Free** (Hex-Rays)
- **radare2 / Cutter** (OSS)
- **Binary Ninja** (commercial)
- **x64dbg / OllyDbg** (Windows debuggers)
- **GDB / LLDB** (Linux/macOS debuggers)
- **dnSpy / ILSpy** — .NET decompile
- **JD-GUI / Procyon** — Java decompile

---

## 13. Training Data Sources for Surrogate-1 Security Capabilities

### 13.1 Public threat intel feeds

- **MISP** (misp-project.org) — open-source threat intel platform; community feeds
  (CIRCL, Botvrij, abuse.ch)
- **OpenCTI** (filigran.io) — STIX 2.1-native platform
- **AlienVault OTX** (now AT&T Cybersecurity) — community pulses
- **Pulsedive** (free tier) — IOC enrichment
- **VirusTotal** (Google) — file/URL reputation, YARA hunting
- **MalwareBazaar** (abuse.ch) — malware sample sharing
- **URLhaus** (abuse.ch) — malicious URLs
- **ThreatFox** (abuse.ch) — IOC database
- **Feodo Tracker** (abuse.ch) — banking trojan C2
- **PhishTank** (Cisco) — phishing URLs

### 13.2 Vulnerability data

- **NVD** (nvd.nist.gov) — CVE + CVSS
- **CVE.MITRE.org** — root registry
- **CISA KEV Catalog** (cisa.gov/known-exploited-vulnerabilities-catalog)
- **GitHub Advisory Database** (github.com/advisories) — best for OSS
- **Open Source Vulnerabilities (OSV)** — Google, OSV format
- **EPSS Data** (first.org/epss/data_stats) — daily probability
- **Exploit-DB** (exploit-db.com) — historical exploits

### 13.3 Detection rule repos

- **SigmaHQ/sigma** (3000+ rules) — *primary training source*
- **elastic/detection-rules** (1000+ EQL/KQL rules)
- **chronicle/detection-rules** (Google)
- **splunk/security_content** (ESCU)
- **falcosecurity/falco** (default rules)
- **Yara-Rules/rules** (1000+ YARA)
- **Neo23x0/signature-base** (Florian Roth's rules)

### 13.4 Cyber LLM training datasets (verified 2025)

| Dataset | Size | Format | Source | License | Use |
|---------|------|--------|--------|---------|-----|
| **Primus-FineWeb** | 2.57B tokens | text | Trend Micro | CC-BY/ODC-By | Continued pre-training |
| **Primus-Seed** | (unspecified) | text | Trend Micro | ODC-By | Pre-training seed |
| **Primus-Instruct** | (mixed) | instruction | Trend Micro | ODC-By | SFT |
| **Primus-Reasoning** | ~4060 samples (CTI tasks w/ o1+R1 reasoning traces) | reasoning | Trend Micro | ODC-By | Reasoning distillation (CISSP +15.8%) |
| **CyberLLMInstruct** | 54,928 pairs | instruction | Univ. research | (academic) | Instruction tuning (CyberMetric 92.5%; **safety drops to 0.15** for prompt injection) |
| **HackMentor** | (varies) | instruction | Tsinghua | (academic) | SFT |
| **CTI-Bench** | 4,060 samples | MCQ + RCM + VSP + ATE | Academic | (academic) | Eval |
| **SecBench** | 44,823 MCQs + 3,087 SAQs | MCQ + short-answer (CN+EN) | Academic | open | Eval |
| **SecEval** | (curated) | MCQ | Academic | open | Eval |
| **CyberMetric** | varies | MCQ severity/actor/response | Academic | open | Eval |
| **SecQA** | varies | MCQ | Academic | open | Eval |
| **CyberSOCEval** | (open) | MCQ malware-analysis + threat-intel reasoning | Meta + CrowdStrike | open | Eval (Sept 2025) |
| **SEC-bench** | (auto-generated) | PoC + patches from real CVEs | Academic (NeurIPS 2025) | open | Eval (best LLM = 18% PoC, 34% patch) |
| **ai4privacy/pii-masking-200k** | 200k | text | HF | open | PII detection |
| **deepset/prompt-injections** | 662 | text | HF | open | Defense training |
| **JailbreakBench** | (varies) | prompts | Academic | open | Safety eval |
| **WildJailbreak** (allenai) | 262k | prompts | Allen AI | open | Safety training |
| **CAIBench** | meta-benchmark of cyber AI agents | MIX | Academic | open | Comprehensive eval |
| **ZeroDayBench** | (varies) | unseen 0day | Academic | open | Generalization |

### 13.5 Adversary emulation / detection validation

- **Atomic Red Team** (Red Canary) — github.com/redcanaryco/atomic-red-team
- **MITRE Caldera** — github.com/mitre/caldera
- **Stratus Red Team** — stratus-red-team.cloud
- **DetectionLab** (Chris Long) — pre-baked Splunk + Velociraptor + Windows lab
- **Splunk Attack Range** — github.com/splunk/attack_range
- **APTSimulator** (Florian Roth)

### 13.6 Specific 2025 papers worth ingesting (or distilling reasoning from)

- *CyberLLMInstruct* (arxiv 2503.09334) — safety vs performance trade-off
- *Primus* (arxiv 2502.11191) — open dataset suite
- *CyberSOCEval* (arxiv 2509.20166) — Meta+CrowdStrike SOC benchmark
- *SEC-bench* (arxiv 2506.11791) — auto-benchmarked LLM agent on real CVEs
- *CyberPal.AI* (arxiv 2408.09304) — expert-driven cyber instructions
- *AutoPen* — autonomous penetration testing w/ LLM agents
- *ZeroDayBench* — unseen-0day generalization
- *CTIBench* — CTI evaluation benchmark
- *CAIBench* (arxiv 2510.24317) — meta-benchmark for cyber AI agents

---

## 14. 2025–2026 LLM-for-Security Tooling Landscape

### 14.1 Commercial AI-SOC platforms

| Vendor | Product | Tagline | Underpinning |
|--------|---------|---------|--------------|
| Microsoft | **Security Copilot** + 6 native agents (Defender, Entra, Intune, Purview) | "Tier-1+ analyst as a service" | GPT-4 + Microsoft security graph |
| CrowdStrike | **Charlotte AI** (Agentic Analyst) | Investigation in seconds | Charlotte LLM + Falcon platform + threat intel |
| SentinelOne | **Purple AI** | Threat hunting natural language | LLM + S1 Singularity Data Lake |
| Google | **Threat Intelligence AI** | Mandiant + VirusTotal + Chronicle queries | Gemini + Mandiant |
| Trend Micro | **Trend Cybertron** (open-source!) | Agentic cyber AI | Llama-Primus-Nemotron-70B + agent |
| Palo Alto | **Cortex XSIAM AI** | Autonomous SOC | proprietary + GPT |
| Splunk (Cisco) | **Splunk AI** | Search + investigate in NL | proprietary |
| Cisco | **Cisco AI Defense** | XDR + AI assist | proprietary |
| Sophos | **Sophos AI** | Endpoint + email + cloud | proprietary |
| IBM | **QRadar Suite + watsonx** | SIEM + AI | watsonx Granite |

### 14.2 Open-source / startup AI-security

- **Trend Cybertron** (open weights) — Llama-Primus-Nemotron-70B
- **Prophet Security** — investigation copilot
- **Dropzone AI** — autonomous SOC
- **D3 Security** — agentic investigation
- **Torq** — AI SOAR (HyperSOAR)
- **Conifers** — AI SOC platform
- **Radiant Security** — AI SOC analyst
- **AppSec Engineer (AquilaX, etc.)** — AI AppSec
- **GPTSecurity** — community/open OSS LLM-sec compendium
- **AutoPenTest** (research) — autonomous pentest agent
- **Garak** (NVIDIA) — LLM red-team harness

### 14.3 LLM security testing (red-teaming the LLM itself)

- **Microsoft PyRIT** (Python Risk Identification Toolkit)
- **NVIDIA Garak** — LLM vuln scanner
- **Promptfoo** — eval + adversarial testing
- **HiddenLayer Mindspy / AISec**
- **Robust Intelligence** (now Cisco) — model firewall

### 14.4 The Honest Truth (for Surrogate-1 v2 design)

Even Llama-Primus-Nemotron-70B (10B+ token continued pre-training, all the reasoning
distillation tricks) achieves only ~76% on CISSP-level and CISSP-is-not-real-world.
**SEC-bench** (real CVE PoC generation + patching) — the best LLM tested **scored 18%
on PoC generation and 34% on patching**. State-of-the-art is **not** reliable autonomous
SOC at any size. Anything Surrogate-1 v2 produces is **assistive / drafts / rule-suggestion**,
**never autonomous response**, never trust-without-verify. Set expectations honestly.

---

## 15. Surrogate-1 v2 Security Training Plan (HONEST)

### 15.1 Realistic capability targets at 7B + LoRA

| Capability | Target | Why it's achievable |
|-----------|--------|---------------------|
| Sigma rule generation given ATT&CK + log source | 75% syntactically valid + lint-pass | Pattern-matching task, ample data |
| YARA rule from sample malware string set | 70% lint + match-true-pos sample | Syntax-bound |
| Falco rule from K8s/syscall description | 70% syntactically valid | Smaller domain |
| ATT&CK technique ID given attack description | 80% top-1, 95% top-3 | Direct retrieval |
| CVE risk explanation + remediation guidance | 75% CVSS+EPSS+KEV-aware | Numeric reasoning |
| AWS misconfiguration → IAM policy fix | 70% policy-correct | Codebase-aware via existing v1 RAG |
| IR runbook step-by-step for common scenarios (ransomware/BEC/RDP-bruteforce) | High coverage on top-10 scenarios | Recipe-following |
| Compliance control crosswalk (SOC2 ↔ ISO ↔ NIST) | 80% accurate mapping | Tabular knowledge |
| Secure code review (SAST-style finding explanation) | 70% on Top-10 CWE | Code understanding |
| Threat intel summarization (CTI report → IOCs + TTPs) | 65% CTI-Bench | Within reach |

### 15.2 Capabilities **OUT OF SCOPE** for v2

- Autonomous incident response (no model below 70B is safe)
- Real-time alert triage at scale (latency + safety)
- Malware reverse engineering (specialized RE LLM needed)
- Exploit generation (refusal-trained; would require red-team alignment removal — DON'T)
- Kerberos / AD attack chain execution (offensive enablement risk)
- Certified pentesting (need OSCP-level reasoning, not yet feasible at 7B)
- Adversarial robustness against prompt injection (CyberLLMInstruct showed safety
  collapses post-fine-tune — need strong post-training safety pass)

### 15.3 Dataset mix for v2 LoRA training

| Phase | Dataset | Tokens | % of total | Purpose |
|-------|---------|--------|-----------|---------|
| **Continued pre-training (optional, 5–10B tokens)** | Primus-FineWeb (filtered) | 2.57B | 30% | Cyber vocab + KCs |
|  | NIST 800-series + ISO 27001/2 + CIS Controls + PCI DSS + HIPAA + GDPR text | ~50M | 5% | Compliance grounding |
|  | MITRE ATT&CK STIX bundles (full v18) + descriptions | ~30M | 3% | TTP grounding |
|  | SigmaHQ rules + Elastic detection rules + falco + YARA | ~80M | 10% | Detection patterns |
|  | CISA KEV + NVD CVEs + GitHub Advisories | ~150M | 15% | Vuln knowledge |
|  | DFIR Blue Team Cheatsheets + SANS posters | ~20M | 3% | Quick-reference internalization |
|  | Atomic Red Team + Caldera abilities + Stratus | ~30M | 4% | Adversary emulation |
|  | OWASP Top 10 + API Top 10 + LLM Top 10 + ASVS | ~10M | 2% | App security |
|  | AWS Security guidance (whitepapers, IR runbooks) | ~50M | 5% | Cloud security |
|  | Filtered cyber subreddits + blog corpus (non-toxic) | ~200M | 18% | Real-world tone |
|  | Multilingual cyber (TH+EN, important for user) | ~50M | 5% | Bilingual capability |
| **SFT (instruction tuning, ~100k examples)** | Primus-Instruct | ~30k pairs | 30% | Cyber instruction baseline |
|  | CyberLLMInstruct (filtered for safety) | ~25k pairs | 25% | Diverse cyber tasks |
|  | HackMentor | ~10k pairs | 10% | RE + offensive understanding (defensive-only refactor) |
|  | Synthetic Sigma generation (ATT&CK→rule) | ~10k pairs | 10% | Detection engineering |
|  | Synthetic compliance Q&A (control crosswalks) | ~5k pairs | 5% | GRC questions |
|  | AWS-specific secure-code review (CDK + IAM + S3 + KMS) | ~8k pairs | 8% | Codebase-aware |
|  | IR playbook step generation (top 20 scenarios × 50 var) | ~5k pairs | 5% | IR fluency |
|  | LLM-Top-10 defense scenarios | ~3k pairs | 3% | LLM AppSec |
|  | Bilingual (TH) cyber Q&A | ~5k pairs | 5% | User language |
| **Reasoning distillation (Primus-Reasoning style)** | CTI-Bench reasoning traces from o1/R1/DeepSeek-R1 | ~4060 samples | n/a | CISSP +15.8% lift target |
| **Safety post-training** | WildJailbreak + ai4privacy + custom security-refusal pairs | ~20k pairs | n/a | Re-instate refusal post-fine-tune |

### 15.4 Eval suite (the bench Surrogate-1 v2 must pass)

| Benchmark | Target score | Why |
|-----------|-------------|-----|
| **CyberMetric** | ≥75% | broad cyber knowledge |
| **CTI-Bench** (MCQ + RCM + VSP + ATE) | ≥65% | threat intel reasoning |
| **SecBench English MCQ** | ≥60% | breadth |
| **SecEval** | ≥60% | sw/net/web sec |
| **CyberSOCEval** (malware analysis + TI reasoning) | ≥55% | Meta+CrowdStrike SOC benchmark |
| **CISSP practice exams** | ≥70% | certification knowledge baseline |
| **Custom Sigma-rule eval** (input: attack desc + log source → output: Sigma) | ≥70% syntactic + ≥50% semantic match | core skill |
| **Custom CDK secure-code review** (user's `cdk-infrastructure/`) | ≥75% (vs reviewer agent ground truth) | codebase-aware |
| **Custom IR runbook eval** (scenario → steps) | ≥75% step recall | playbook fluency |
| **Safety eval** (prompt-injection refusal, exploit refusal) | ≥80% (post-safety pass) | non-regression |
| **PII detection (ai4privacy held-out)** | ≥85% F1 | data protection |

### 15.5 Architecture additions to v1 to enable Sec capability

```
Surrogate-1 v1                    Surrogate-1 v2
─────────────                     ─────────────
Qwen2.5-Coder-7B                  Qwen2.5-Coder-7B (same base)
  + DevOps LoRA                     + DevSecOps LoRA (merged)
  + CDK RAG                         + CDK RAG
                                    + ATT&CK STIX + KEV + EPSS RAG (NEW)
                                    + SigmaHQ + Elastic rules RAG (NEW)
                                    + Compliance crosswalk DB (NEW)
                                    + AWS Security guidance RAG (NEW)
  + Tool: bash, python              + Tool: bash, python
                                    + Tool: prowler scan-runner (NEW — already in user infra)
                                    + Tool: trivy scanner (NEW)
                                    + Tool: sigma-cli compile-and-test (NEW)
                                    + Tool: cfn-guard / checkov runner (NEW)
                                    + Tool: gh advisory lookup (NEW)
                                    + Tool: VirusTotal hash lookup (NEW, opt-in API key)
                                    + Tool: MITRE ATT&CK API navigator (NEW)
```

The user's existing `cdk-infrastructure/prowler-scan/` biweekly Prowler cron is the
**flagship integration target**: Surrogate-1 v2 should be able to ingest Prowler JSON
output and produce a remediation Jira-ready report mapped to CIS/NIST/ISO controls,
with CDK-aware fix patches.

---

## 16. Concrete Examples — what "good" Surrogate-1 output looks like

### 16.1 Sigma rule generation

**Input**: *"Detect adversary using `certutil.exe` to download a payload over HTTP from
a non-corporate domain on Windows endpoints."*

**Expected output**:

```yaml
title: Suspicious certutil.exe Download Activity
id: <new-uuid-v4>
status: experimental
description: Detects use of certutil.exe with -urlcache or -split parameters to download
  files from external HTTP/S URLs, a common LOLBin technique (T1105 Ingress Tool Transfer
  + T1218 Signed Binary Proxy Execution).
references:
  - https://lolbas-project.github.io/lolbas/Binaries/Certutil/
  - https://attack.mitre.org/techniques/T1105/
author: Surrogate-1 (auto-generated, human-review-required)
date: 2026-04-29
tags:
  - attack.command-and-control
  - attack.t1105
  - attack.t1218
logsource:
  product: windows
  category: process_creation
detection:
  selection_img:
    Image|endswith: '\certutil.exe'
  selection_args:
    CommandLine|contains|all:
      - 'certutil'
    CommandLine|contains:
      - '-urlcache'
      - '-split'
      - 'http://'
      - 'https://'
  filter_corp_domain:
    CommandLine|contains:
      - 'http://intranet.corp.local'
      - 'https://wsus.corp.local'
  condition: selection_img and selection_args and not filter_corp_domain
falsepositives:
  - Legitimate certificate management on isolated test endpoints
  - Patch staging via internal HTTP server (handled by filter_corp_domain)
level: high
```

### 16.2 IR runbook step generation

**Input**: *"User reports their account in AWS Console shows IAM activity from an unfamiliar
country. What do I do?"*

**Expected output**:

```markdown
# Suspected AWS IAM Account Compromise — IR Steps

## 0. Verify the report
- Pull CloudTrail events for the IAM principal in the last 7 days:
  `aws cloudtrail lookup-events --lookup-attributes AttributeKey=Username,AttributeValue=<user>`
- Check `sourceIPAddress`, `userAgent`, geolocation against user's known patterns
- Confirm GuardDuty findings: `aws guardduty list-findings --detector-id <id>` — look for
  `UnauthorizedAccess:IAMUser/ConsoleLoginSuccess.B` (anonymizing proxy) or
  `CredentialAccess:IAMUser/AnomalousBehavior`

## 1. CONTAIN (immediate)
- Disable the user's access keys: `aws iam update-access-key --access-key-id <AKIA...> --status Inactive --user-name <user>`
- Force console password reset (deny re-login): `aws iam delete-login-profile --user-name <user>`
- Detach all policies temporarily: capture current via `aws iam list-attached-user-policies`
  + `aws iam list-user-policies` then `aws iam detach-user-policy` / `aws iam delete-user-policy`
- Add deny-all SCP at OU/account level if cross-account exposure suspected
- DO NOT delete the user yet — preserve forensic state

## 2. SCOPE
- Pull all events authored by this principal: `cloudtrail lookup-events` for 30d
- Check assumed roles via session credentials: `cloudtrail lookup-events
  --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=<ASIA...>`
- IAM Access Analyzer: `aws accessanalyzer list-findings` for any external sharing newly
  added by this principal
- Check if MFA was bypassed: `aws iam get-login-profile`, `aws iam list-mfa-devices`
- Check S3 / KMS / RDS data plane events for exfil indicators

## 3. ERADICATE
- Rotate ALL credentials touched by this principal (other users' creds if cross-impersonation)
- Force MFA re-registration for the user
- Audit IAM changes (CreateUser, AttachPolicy, CreateAccessKey, AssumeRole) made by this
  principal — REVERT each
- Review/revoke all OAuth grants if SSO-federated
- Check for new IAM roles with trust policies allowing the attacker's external account

## 4. RECOVER
- Re-enable user with new password + new access keys (if needed) only after MFA re-registration
- Validate normal CloudTrail patterns for 14 days
- Add custom detection: GuardDuty filter for this user's normal-region pattern

## 5. NOTIFY
- Internal: security ticketing (Jira SEC-XXXX), CISO if scope > 1 account
- Customer-impacting: legal review under contract notice clauses
- Regulatory: GDPR 72h if EU PII data plane event observed; HIPAA 60d if PHI

## 6. LESSONS
- Update detection: `cloudtrail::ConsoleLogin` from country list anomaly per user
- Add to IR runbook: this case as an exemplar
- Tabletop within 30d to validate the runbook
```

### 16.3 Compliance control crosswalk

**Input**: *"For ISO 27001 A.8.5 (Secure authentication), what's the SOC 2 equivalent and what AWS evidence proves the control?"*

**Expected output**:

```markdown
ISO 27001:2022 Annex A.8.5 — Secure authentication
↔ SOC 2 Trust Services Criteria CC6.1 (logical access)
↔ NIST 800-53 IA-2 (Identification and Authentication, Organizational Users)
↔ NIST CSF 2.0 PR.AA-03 (Users, services, and hardware are authenticated)
↔ PCI-DSS 4.0.1 Req 8.4 (MFA)
↔ HIPAA 164.312(d) (Person or entity authentication)
↔ CIS Controls v8 6.5 (MFA for administrative access)

AWS evidence:
- AWS IAM Identity Center (formerly SSO) configured with MFA-required
- IAM Account-level password policy: `aws iam get-account-password-policy` showing
  MinimumPasswordLength ≥14, RequireSymbols, RequireNumbers, RequireUppercase,
  RequireLowercase, MaxPasswordAge ≤90, ReuseRevention ≥24
- IAM users with MFA enabled: query `IAMUserCredentialsReport`
- AWS Config rule: `iam-user-mfa-enabled`, `mfa-enabled-for-iam-console-access`,
  `root-account-mfa-enabled` all COMPLIANT
- CloudTrail evidence of MFA at sign-in: `userIdentity.sessionContext.attributes.mfaAuthenticated == true`
- AWS Security Hub control AWS Foundational Security Best Practices:
  IAM.5 (MFA for users with console password) — PASSED
  IAM.6 (Hardware MFA for root) — PASSED
  IAM.19 (MFA enabled for all IAM users with console password) — PASSED
- AWS Audit Manager evidence collection (auto): assessment for ISO 27001 framework

Recommended Surrogate-1 follow-up:
1. Run Prowler check: `prowler aws --compliance iso27001_2022 --severity high`
2. Generate SCP for organizations to enforce MFA: `aws:MultiFactorAuthPresent: true`
3. Add CDK construct enforcement (review user's `cdk-infrastructure/` for IAM constructs missing MFA condition)
```

---

## 17. Real CVE Analysis Example

**CVE-2024-3094 (XZ Utils Backdoor)** — used as Surrogate-1 training exemplar

```markdown
# CVE-2024-3094 — XZ Utils Backdoor Analysis

## Summary
Malicious code (CVSS 10.0) inserted into XZ Utils (liblzma) versions 5.6.0 and 5.6.1 by
threat actor "Jia Tan" (long-running social engineering operation against the maintainer).
The backdoor hooks SSH daemon authentication via systemd → libsystemd → liblzma loaded
via auditd, allowing remote code execution with the appropriate private key.

## Affected
- xz-utils 5.6.0, 5.6.1 (released Feb–Mar 2024)
- Distributions that shipped these: Fedora 41/Rawhide, Debian unstable, openSUSE Tumbleweed,
  Kali Linux, Arch Linux, Alpine Linux edge

## Detection
- File hash IOCs:
  - `liblzma.so.5.6.1` SHA256 ranges (multiple variants)
- Behavioral: SSH process making unusual liblzma calls during auth
- YARA rules:

```yara
rule XZ_Utils_Backdoor_Function {
    meta:
        author = "Vegard Nossum / Andres Freund"
        date = "2024-03-29"
        reference = "CVE-2024-3094"
    strings:
        $func1 = { 48 89 5C 24 ?? 48 89 6C 24 ?? 48 89 74 24 ?? 41 56 41 57 48 83 EC 30 }
        $func2 = "_get_cpuid"
        $marker = "Jia Tan"
    condition:
        uint32(0) == 0x464c457f and 2 of them
}
```

## Remediation
- **Pin/downgrade** to xz-utils 5.4.6 or earlier
- Distro-specific: `apt-get install --reinstall xz-utils=5.4.5-0.3` (Debian)
- Verify package signatures + rebuild from clean source where possible
- Audit any system that ran 5.6.0/5.6.1 + sshd between Feb–Mar 2024 for unauthorized SSH access

## Lessons (for this CVE class)
- Long-tail social engineering of single-maintainer OSS projects is a real supply-chain risk
- Test build artifacts vs source repo for divergence (build provenance = SLSA L3+)
- Sigstore / cosign signing of release tarballs would have caught binary-only backdoor

## Surrogate-1 reasoning steps
1. Identify package manager queries to find affected systems:
   `dpkg -l | grep xz-utils`, `rpm -qa | grep xz-utils`, `apk info xz`
2. Generate Sigma rule for sshd anomalous auth latency (the backdoor adds ~500ms)
3. Generate AWS SSM Patch Manager remediation for fleet
4. Generate post-mortem template referencing SLSA + Sigstore as forward control
```

---

## 18. Hands-on Real Tools Reference (for Surrogate-1 to know commands)

```bash
# === Prowler (AWS-focused, used in user's infra) ===
prowler aws -M json-asff -o /tmp/prowler-out
prowler aws --compliance soc2_2022 --severity critical,high
prowler aws --service guardduty --service iam
prowler aws --check-id iam_root_mfa_enabled

# === Trivy ===
trivy image --severity HIGH,CRITICAL --format json alpine:3.18
trivy fs --scanners vuln,secret,config .
trivy k8s --report summary cluster
trivy sbom result.cdx.json     # use SBOM as input

# === Checkov ===
checkov -d . --framework terraform,cloudformation,kubernetes --output sarif
checkov -f stack.template.yaml --quiet

# === Semgrep ===
semgrep --config=p/owasp-top-ten --config=p/security-audit --json .
semgrep --config=auto .

# === Sigma ===
sigma convert -t splunk rules/                     # SPL output
sigma convert -t lucene rules/                     # Elastic
sigma convert -t azure-monitor rules/              # Sentinel KQL
pysigma test rule.yml                              # validate

# === YARA ===
yara -r rules/ /path/to/scan
yara --print-strings rules/ sample.bin

# === Volatility 3 ===
vol -f mem.raw windows.info
vol -f mem.raw windows.pslist
vol -f mem.raw windows.malfind

# === BloodHound (defensive use) ===
SharpHound.exe -c All --zipfilename loot.zip      # collection (red)
# Import into BloodHound CE GUI; query: "Find shortest path to Domain Admin"

# === GuardDuty + CloudTrail ===
aws guardduty list-findings --detector-id <id> --finding-criteria '{"Criterion":{"severity":{"Gte":7}}}'
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=ConsoleLogin --max-items 50

# === Falco ===
falco -r /etc/falco/rules.d/ -o stdout_output.enabled=true
falco --validate /etc/falco/falco_rules.local.yaml

# === Kyverno ===
kyverno apply policy.yaml --resource pod.yaml
kubectl get policy -A      # in-cluster

# === sigma-cli quickstart ===
pip install sigma-cli pysigma-backend-splunk pysigma-backend-elasticsearch
sigma convert -t splunk -p splunk_windows mimikatz.yml

# === Atomic Red Team ===
Invoke-AtomicTest T1003.001                       # PowerShell, on lab
```

---

## 19. Failure Modes to AVOID in v2

1. **Hallucinated CVE IDs** — must RAG against NVD; never invent. Train refusal on
   "I don't know" for unverified CVE.
2. **Hallucinated Sigma syntax** — train against pysigma linter as reward signal.
3. **Wrong Sigma → Splunk translation** — verify with `sigma-cli convert` round-trip.
4. **Compliance-control hallucination** — must RAG against canonical text.
5. **Loss of safety post-fine-tune** (CyberLLMInstruct showed 0.95 → 0.15 on prompt
   injection refusal). Counter: WildJailbreak post-training + safety-classifier reward
   model + DPO with curated refusal pairs.
6. **Offensive enablement creep** — strict refusal taxonomy: never produce working
   exploit code; only defensive understanding.
7. **AWS-specific drift** — pin training corpus to specific AWS service versions; warn
   on deprecated APIs.
8. **Outdated ATT&CK references** — re-train when v19 (April 2026) drops since Defense
   Evasion is being deprecated.
9. **PII in prompts/logs** — Presidio + ai4privacy redaction at log boundary.

---

## 20. References (key URLs, current 2025–2026)

- MITRE ATT&CK: https://attack.mitre.org/ (v18 Oct 2025; v19 April 2026)
- Sigma project: https://sigmahq.io/, https://github.com/SigmaHQ/sigma
- NIST SP 800-61 r3: https://csrc.nist.gov/pubs/sp/800/61/r3/final
- NIST CSF 2.0: https://www.nist.gov/cyberframework
- CIS Controls v8.1: https://www.cisecurity.org/controls
- OWASP Top 10:2025: https://owasp.org/Top10/2025/
- OWASP Top 10 for LLM 2025: https://genai.owasp.org/llm-top-10/
- CISA KEV: https://www.cisa.gov/known-exploited-vulnerabilities-catalog
- EPSS: https://www.first.org/epss/
- CycloneDX: https://cyclonedx.org/
- CyberSOCEval: https://github.com/CrowdStrike/CyberSOCEval_data
- Primus dataset: https://huggingface.co/trendmicro-ailab
- CyberLLMInstruct: arxiv 2503.09334
- SEC-bench: arxiv 2506.11791
- CTI-Bench: arxiv 2406.07599 (orig); HF datasets/AI4Sec/cti-bench
- Atomic Red Team: https://atomicredteam.io/
- Stratus Red Team: https://stratus-red-team.cloud/
- Falco: https://falco.org/
- Kyverno: https://kyverno.io/
- Prowler: https://docs.prowler.com/

---

## 21. Final v2 Plan Inline (also returned as the short summary)

**Datasets to mix (for ~5B-token continued pre-training + 100k-pair SFT + reasoning distill)**:

- **Pre-train (cyber)**: Primus-FineWeb (2.57B), MITRE ATT&CK STIX, NIST/ISO/CIS/PCI/HIPAA
  text, CISA KEV + NVD, SigmaHQ + Elastic detection-rules + Falco + YARA, Atomic Red Team +
  Caldera, OWASP Top 10 family, AWS Security guidance.
- **SFT**: Primus-Instruct, CyberLLMInstruct (safety-filtered), HackMentor (refactored
  defensive-only), synthetic Sigma generation (10k), compliance crosswalk Q&A (5k),
  AWS-CDK secure-code review (8k), IR playbook step gen (5k), LLM-Top-10 defense (3k),
  bilingual TH+EN cyber Q&A (5k).
- **Reasoning distill**: Primus-Reasoning (CTI-Bench reasoning traces, ~4k samples).
- **Safety post-training**: WildJailbreak (subset) + ai4privacy + custom security-refusal
  pairs (~20k) — counter the CyberLLMInstruct safety collapse.

**Eval targets** (the bench v2 must clear):
CyberMetric ≥75% • CTI-Bench ≥65% • SecBench EN MCQ ≥60% • SecEval ≥60% •
CyberSOCEval ≥55% • CISSP practice ≥70% • Sigma-rule synthesis ≥70% lint + ≥50% semantic •
Custom CDK secure-review ≥75% • IR runbook step recall ≥75% • Safety eval ≥80% •
PII detection F1 ≥85%.

**Infra additions**: ATT&CK + KEV + EPSS RAG, SigmaHQ rules RAG, compliance crosswalk DB,
new tools (Prowler runner, Trivy, sigma-cli compile, cfn-guard, gh advisory, VirusTotal,
ATT&CK Navigator). Reuse v1 codebase RAG for CDK awareness.

**Out of scope (be honest)**: autonomous IR, real-time alert triage, malware RE,
exploit/0day generation, AD attack chain execution, certified pentesting. Surrogate-1 v2
is a **DevSecOps assistant**, not an autonomous SOC.
