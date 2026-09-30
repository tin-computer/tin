-- Project-owned X OAuth grants and delivery records use the existing integration tables.
ALTER TABLE integration_connections DROP CONSTRAINT integration_connections_provider_key_check;
ALTER TABLE integration_connections ADD CONSTRAINT integration_connections_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_call_receipts DROP CONSTRAINT integration_call_receipts_provider_key_check;
ALTER TABLE integration_call_receipts ADD CONSTRAINT integration_call_receipts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google',
                            'payments.stripe', 'analytics.posthog', 'infra.github_user', 'social.x')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_auth_attempts DROP CONSTRAINT integration_auth_attempts_provider_key_check;
ALTER TABLE integration_auth_attempts ADD CONSTRAINT integration_auth_attempts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google',
                            'analytics.posthog', 'infra.github_user', 'social.x'));
