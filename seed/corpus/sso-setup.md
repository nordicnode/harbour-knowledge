# Setting up SSO

## Availability

SSO is available on the Enterprise plan only. Team and Business workspaces cannot enable SAML single sign-on.

## Okta setup

1. In Harbour, open Settings then Security and choose Enable SAML.
2. In Okta, create a new SAML application and use the metadata URL shown in Harbour.
3. Map the email attribute to the Okta user email.
4. Test with a pilot group before enforcing SSO for everyone.

## Troubleshooting

If users see "SAML response invalid", check that the Okta clock is synchronised and that the email attribute is present.
