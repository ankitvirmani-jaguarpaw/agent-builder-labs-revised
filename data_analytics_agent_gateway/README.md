# Data Analytics Agent with Google Cloud Agent Gateway

Comprehensive enterprise architecture and turnkey deployment for the **Data Analytics Agent** governed by **Google Cloud Agent Gateway** (Ingress and Egress), **Model Armor**, and **Cloud DLP**.

---

## 🏛️ Architecture Overview

```
                                      [ Client / User ]
                                              │
                                              ▼
                             ┌─────────────────────────────────┐
                             │    Ingress Agent Gateway        │
                             │ (fsi-agent-gateway-ingress)     │
                             │  • CLIENT_TO_AGENT              │
                             │  • Model Armor Content Authz    │
                             │  • Prompt-Injection Protection  │
                             │  • DLP SSN De-identification    │
                             └────────────────┬────────────────┘
                                              │
                                              ▼
                             ┌─────────────────────────────────┐
                             │    Vertex AI Agent Engine       │
                             │ (data_analytics_agent_gateway)  │
                             │  • AGENT_IDENTITY Principal     │
                             │  • ADK 2.x Runtime              │
                             │  • Tokenomics Governance        │
                             └────────────────┬────────────────┘
                                              │ (TLS Inspected)
                                              ▼
                             ┌─────────────────────────────────┐
                             │    Egress Agent Gateway         │
                             │ (fsi-agent-gateway-egress)      │
                             │  • AGENT_TO_ANYWHERE            │
                             │  • Agent Registry Governance    │
                             │  • IAP Verification (Dry-Run)   │
                             └──────┬───────────────────┬──────┘
                                    │                   │
                  ┌─────────────────┴─┐               ┌─┴─────────────────┐
                  ▼                   ▼               ▼                   ▼
       [ Knowledge Catalog MCP ]  [ BigQuery ]   [ Judge Agent (A2A) ]  [ Vertex AI ]
           (Cloud Run SSE)                          (Cloud Run A2A)
```

---

## 📂 Directory Layout

```
data_analytics_agent_gateway/
├── cfg/
│   ├── agw-ssn-inspect-template.json             # DLP Inspect template for SSN
│   ├── agw-ssn-redaction-template.json           # DLP De-identify template for SSN redaction
│   ├── agw-request-template.json                 # Model Armor template (Prompt Injection / Malicious URIs)
│   ├── agw-response-template.json                # Model Armor template (DLP SSN Redaction)
│   ├── fsi-agent-gateway-ingress.yaml            # Ingress Agent Gateway specification (CLIENT_TO_AGENT)
│   ├── fsi-agent-gateway-egress.yaml             # Egress Agent Gateway specification (AGENT_TO_ANYWHERE)
│   ├── fsi-agent-gateway-ma-authz.yaml           # Model Armor Authz Service Extension manifest
│   ├── fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun.yaml  # Egress IAP Authz Service Extension
│   ├── fsi-agent-gateway-ingress-ma-policy.json  # Ingress Authz Policy (CONTENT_AUTHZ -> Model Armor)
│   └── fsi-agent-gateway-egress-iap-policy.json  # Egress Authz Policy (IAP Dry-Run)
├── scripts/
│   ├── setup_gateways_and_security.sh            # Provisions DLP, Model Armor, Gateways, Extensions & CA bundle
│   └── sync_agent_registry.sh                    # Synchronizes A2MCP and A2A service URLs with Agent Registry
├── __init__.py                                   # ADK package init
├── agent.py                                      # Governed Data Analytics Agent implementation
├── requirements.txt                              # Pinned container runtime dependencies
├── .agent_engine_config.json                     # Infrastructure wiring for Agent Engine & Gateways
├── .env                                          # Configured environment variables and CA bundle paths
├── .env.example                                  # Template for environment variables and SSL bundle
├── gateway-ca.crt                                # CA certificate bundle for Egress TLS inspection
├── deploy.sh                                     # Turnkey end-to-end deployment & IAM automation
├── verify.sh                                     # End-to-end verification and audit log check
├── test_agent.py                                 # Local mock runner test
└── README.md                                     # This documentation
```

---

## 🚀 Quickstart & Deployment

### 1. Execute Turnkey Deployment
To provision all infrastructure (DLP, Model Armor, Ingress/Egress Agent Gateways, Service Extensions, Authz Policies, CA certificate bundle, Agent Registry sync), deploy the agent, and bind all IAM roles:

```bash
cd ~/agent-builder-labs-revised
./data_analytics_agent_gateway/deploy.sh
```

### 2. Updating an Existing Deployment
To update an existing Reasoning Engine instance in-place:

```bash
./data_analytics_agent_gateway/deploy.sh --agent-engine-id <RE_ID>
```

### 3. Interactive Chat with the Deployed Agent
Start a real-time conversational session in your terminal with live streaming and visual gateway indicators:

```bash
cd ~/agent-builder-labs-revised/data_analytics_agent_gateway
python3 chat.py
```

### 4. Verification & Live Auditing
Run the end-to-end functionality and verification suite:

```bash
./data_analytics_agent_gateway/verify.sh
```

### 5. Active Policy Enforcement Test Suite (No Dry Run)
Run the dedicated enforcement suite to verify that attacks and unauthorized egress are actively rejected:

```bash
./data_analytics_agent_gateway/test_enforcement.sh
```

### 6. Agent Identity & Gateway Interception Test Suite
Verify that the deployed agent principal (`principal://agents.global.org-.../reasoningEngines/4238207756995133440`) has all IAM bindings and is cryptographically verified via mTLS across the Egress Gateway to MCP and Judge Agent:

```bash
./data_analytics_agent_gateway/test_identity_governance.sh
```

### 7. End-to-End Ingress & Egress Negative Scenarios Suite
Comprehensive verification testing active boundary blocking on **both Ingress and Egress**:
- **Ingress**: Prompt injections, credential exfiltration, and unauthenticated tokens rejected (HTTP 401/403).
- **Egress**: Unregistered MCP destinations, unauthorized A2A judge calls, and unauthorized BigQuery tables actively intercepted and blocked.
- Automatically restores registry and IAM permissions upon completion.

```bash
./data_analytics_agent_gateway/test_e2e_negative_scenarios.sh
```


---

## 🛡️ Key Security & Governance Features

1. **Egress TLS Inspection CA Trust**:
   - Outbound traffic from Agent Engine is intercepted by `fsi-agent-gateway-egress`.
   - The deployment script extracts the gateway root CA and appends the complete Mozilla/Google Trust Services bundle (certifi) into `gateway-ca.crt`.
   - Environment variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`) ensure all Python HTTP/HTTPS clients (FastMCP SSE and RemoteA2aAgent) trust the intercepted TLS gateway proxy.

2. **Ingress Model Armor & DLP Sanitization**:
   - The ingress gateway evaluates prompts against `agw-request-template` to block jailbreak and prompt injection attempts.
   - Outputs are processed by `agw-response-template` using DLP to redact sensitive data (e.g. US Social Security Numbers).

3. **Destination Governance via Agent Registry**:
   - `fsi-agent-gateway-egress` validates destination URLs against registered `mcpServers` and `agents`.
   - `sync_agent_registry.sh` ensures dynamic Cloud Run URLs are always synchronized.

4. **Agent Identity Principal IAM**:
   - The deployed Reasoning Engine principal (`principal://agents.global.org-.../reasoningEngines/<RE_ID>`) is granted:
     - `roles/networkservices.agentGatewayUser` on the egress gateway.
     - `roles/bigquery.jobUser` and `roles/bigquery.dataViewer` on the project.
     - `roles/run.invoker` on downstream Cloud Run services.
