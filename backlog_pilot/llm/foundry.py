"""Claude on Microsoft Foundry: runs in the customer's Azure tenant.

With a "Hosted on Azure" deployment, prompts and completions stay within Azure. Authenticate
with an Azure API key, or with Microsoft Entra ID (managed identity, workload identity, or a
developer login) so no key is stored at all.
"""

import os

import anthropic

from backlog_pilot.llm.anthropic_provider import AnthropicProvider

ENTRA_SCOPE = "https://ai.azure.com/.default"
CLIENT_FALLBACK_MODEL = "claude-opus-4-8"  # client-side fallback target if a request is refused


def foundry_client() -> anthropic.AnthropicFoundry:
    """Build the client from ANTHROPIC_FOUNDRY_RESOURCE (or _BASE_URL) plus either
    ANTHROPIC_FOUNDRY_API_KEY or, when no key is set, an Entra ID token from azure-identity."""
    middleware = [anthropic.BetaRefusalFallbackMiddleware([{"model": CLIENT_FALLBACK_MODEL}])]
    if os.getenv("ANTHROPIC_FOUNDRY_API_KEY"):
        return anthropic.AnthropicFoundry(middleware=middleware)
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as e:
        raise RuntimeError(
            "Set ANTHROPIC_FOUNDRY_API_KEY, or install azure-identity "
            "(pip install 'backlog-pilot[azure]') to sign in with Entra ID.") from e
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), ENTRA_SCOPE)
    return anthropic.AnthropicFoundry(azure_ad_token_provider=token_provider,
                                      middleware=middleware)


def foundry_provider(deployment: str) -> AnthropicProvider:
    """`deployment` is the Foundry deployment name (by default the model id)."""
    return AnthropicProvider(deployment, client=foundry_client(), name="foundry",
                             server_fallbacks=False)
