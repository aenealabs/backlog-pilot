# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in Backlog Pilot, please report it privately. **Do not open a
public GitHub issue for security vulnerabilities.**

Email **security@aenealabs.com** with:

- A description of the vulnerability
- Steps to reproduce it
- The impact you expect it to have
- A suggested fix, if you have one

## Response timeline

| Action | Timeline |
|--------|----------|
| Acknowledgment of your report | Within 48 hours |
| Initial assessment | Within 5 business days |
| Fix | Based on severity |
| Public disclosure | After the fix is released |

## Supported versions

Security fixes are made on the `main` branch. Please reproduce issues against the latest
`main` before reporting.

## Scope

In scope: the code in this repository, including its web routes, authentication and
authorization, data handling, and its integrations with other Mission Control apps.

Out of scope: the fictional demo data, vulnerabilities in third-party dependencies that are
already publicly known (report those upstream), and issues that need physical access to a
user's machine.
