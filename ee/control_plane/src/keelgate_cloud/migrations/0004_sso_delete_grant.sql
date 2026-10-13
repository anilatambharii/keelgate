-- Removing an org SSO configuration needs DELETE, which 0001 did not grant on sso_configs.
GRANT DELETE ON sso_configs TO __APP_ROLE__;
