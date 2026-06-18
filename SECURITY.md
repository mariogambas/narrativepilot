# Security Policy

## Supported Versions

NarrativePilot AI is currently in active development for the BNB Hack 2026 hackathon. The following versions are maintained:

| Version | Supported |
|---------|-----------|
| main (latest) | ✅ |
| older commits | ❌ |

---

## Reporting a Vulnerability

If you discover a security vulnerability in NarrativePilot AI, please report it responsibly. **Do not open a public GitHub issue** for security vulnerabilities, as this could expose users to risk before a fix is available.

### How to report

Send an email to: **narrativepilotai@gmail.com**

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce the issue
- Any relevant code snippets, logs, or screenshots
- Your suggested fix (optional but appreciated)

You can expect an acknowledgement within **48 hours** and a resolution timeline within **7 days** for critical issues.

---

## Security Considerations for Self-Hosted Deployments

NarrativePilot AI interacts with real blockchain assets in mainnet mode. If you deploy this agent with real capital, please follow these guidelines:

### Private key and wallet management

- **Never commit your `.env` file to version control.** The `.gitignore` in this repository excludes `.env` by default — do not override this.
- The agent uses **Trust Wallet Agent Kit (TWAK)** for transaction signing. TWAK stores the agent wallet key encrypted in your OS keychain — it never appears in plaintext in any config file or log.
- If you suspect your TWAK wallet has been compromised, immediately transfer any remaining funds to a new wallet and revoke the agent's access by re-running `twak setup` with a new wallet.

### API key protection

- Your `CMC_API_KEY` should be treated as a secret. Do not share it, log it, or commit it.
- If exposed, regenerate it immediately at https://pro.coinmarketcap.com.

### Risk management

- The agent enforces hard limits (stop-loss at -8%, portfolio drawdown cap at -20%, gas reserve) to reduce financial risk, but these are software guardrails and do not guarantee against loss.
- Always test with a small amount of capital before deploying at scale.
- Monitor the dashboard and `logs/trades.log` regularly during live operation.

### Network and RPC

- The agent connects to the CMC Agent Hub MCP endpoint and BSC mainnet via TWAK's built-in RPC. No custom RPC endpoint is exposed by default.
- Do not expose the dashboard (`python -m http.server 8080`) to the public internet — it is intended for local monitoring only.

---

## Known Limitations

- The agent has no authentication layer on the local dashboard — anyone with network access to port 8080 on the host machine can view portfolio data.
- Log files (`logs/trades.log`) contain trade history and portfolio values in plaintext — ensure they are not accessible to untrusted parties.
- The forced daily trade mechanism executes a small position even in unfavorable conditions to comply with competition rules — this behavior is intentional for the hackathon but should be disabled or reconfigured for production use.

---

## Scope

The following are **in scope** for vulnerability reports:
- Private key or wallet credential exposure
- Unauthorized execution of trades
- Injection vulnerabilities in log parsing or CMC data handling
- Denial-of-service conditions that could prevent the agent from executing stop-losses

The following are **out of scope**:
- Issues with third-party services (CMC API, TWAK, BscScan)
- Smart contract vulnerabilities on BNB Chain itself
- Issues that require physical access to the host machine

---

*Last updated: June 2026*
