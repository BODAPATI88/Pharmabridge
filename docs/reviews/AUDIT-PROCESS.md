PharmaBridge Audit Process

Purpose

Establish a repeatable audit and review process for PharmaBridge.

Audits must be evidence-based.

No audit finding should be accepted without supporting evidence from:

- Git repository
- Platform audit reports
- Kubernetes state
- Release artifacts
- Application behavior

---

Weekly Audit Process

Step 1 - Generate Evidence

Run:

./scripts/platform_audit.sh

Output:

docs/evidence/latest-platform-audit.md

---

Step 2 - Review Evidence

Review areas:

- Platform health
- Kubernetes deployments
- Gateway health
- Worker status
- Docker legacy environment
- Release readiness
- Security observations

---

Step 3 - Create Audit Report

Create:

docs/reviews/YYYY-MM-DD-weekly-review.md

Include:

- Findings
- Risks
- Recommendations
- Priority actions

---

Release Audit Process

Before any release:

Verification

- Git branch verified
- Release checklist completed
- Platform audit executed
- Health endpoints verified
- Rollback strategy confirmed

Deliverable

Create:

docs/releases/release-review-<version>.md

---

Roles and Responsibilities

Gemini

Responsible for:

- Evidence review
- Documentation review
- Architecture review
- Risk identification

Gemini does not approve releases.

---

ChatGPT (CTO)

Responsible for:

- Prioritization
- Architecture decisions
- Risk acceptance
- Release decisions
- Roadmap planning

---

Claude

Responsible for:

- Code implementation
- Refactoring
- Bug fixes
- Pull requests
- Technical execution

---

Audit Principles

1. Evidence before conclusions.
2. No assumptions without verification.
3. Document findings.
4. Track decisions through ADRs.
5. Review weekly.
6. Review before releases.

---

Success Criteria

A successful audit process produces:

- Evidence
- Findings
- Prioritized actions
- Traceable decisions

without requiring direct production access for reviewers.
