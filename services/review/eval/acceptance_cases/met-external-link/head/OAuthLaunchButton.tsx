export function OAuthLaunchButton() {
  return (
    <a href={OAUTH_URL} target="_blank" rel="noopener" className="usa-button">
      Continue with MyMassGov
      <ExternalIcon aria-hidden="true" />
      <span className="usa-sr-only">External. Opens in new tab.</span>
    </a>
  );
}
