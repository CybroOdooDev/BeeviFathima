# BioBridge marketing site

A static site: seven HTML pages, one stylesheet, one small script. No build
step, no framework, no dependencies — upload the folder to any static host
(Netlify, Cloudflare Pages, GitHub Pages, S3, or an nginx `root`).

```
index.html      home
pricing.html    plans, what's included, FAQ
setup.html      public setup guide
contact.html    demo / sales request form
signup.html     registration: free trial, or buy a plan (Stripe Checkout)
check-email.html  "check your inbox" (after registering or paying), with resend
verified.html   where the confirmation link lands; releases the login details
assets/
  styles.css    all styling (light + dark, responsive)
  site.js       app links, menu, contact form, live plans, registration
  favicon.svg
```

## Before you publish

1. **Point it at the app.** In `assets/site.js`, set `APP_URL` to where
   BioBridge runs (e.g. `https://app.biobridge.io`). "Sign in" opens the app's
   login; registration, plans and prices are read from the app's API.
   On the app side set `SITE_URL` to this site's address and add the same
   origin to `CORS_ORIGINS`, e.g.

       SITE_URL=https://biobridge.io
       CORS_ORIGINS=https://biobridge.io

   Without them the browser blocks the site's calls, and emails and Stripe
   send people to the app instead of back here.
2. **Contact form.** A static site can't send mail on its own. Either set
   `FORM_ENDPOINT` to a form service that accepts JSON posts (Formspree,
   Basin, Getform, or your own endpoint), or leave it blank and the form opens
   the visitor's email app addressed to `SALES_EMAIL`.
3. **Fill the placeholders.** Search the HTML for `[` —
   `[COMPANY NAME]`, `[SALES EMAIL]`, `[PHONE NUMBER]`, `[OFFICE ADDRESS]`,
   `[RESPONSE TIME]`, `[SUPPORT LEVEL]`, `[SUPPORTED VERSIONS]`,
   `[TERMS URL]`, `[PRIVACY URL]`.
4. **Prices** are read live from the app (`/api/v1/public/plans`). The
   numbers written into `pricing.html` and `index.html` are only a fallback
   if the app can't be reached. "Buy now" appears only for plans with a
   Stripe Price set (Platform → Plans in the staff console).

## Monthly and yearly billing

The pricing page has a Monthly / Yearly switch, and the sign-up form has a
**Pay Monthly / Pay Yearly** choice (`?billing=year` preselects it). Yearly
prices come from the app's plans (`yearly_price_cents`); a plan with no yearly
price stays monthly-only, and Yearly "buy now" appears only once the plan also
has a yearly Stripe Price. Set both in the staff console under Platform → Plans.
A free trial remembers the choice and bills that way when the account buys.

## How registration works

- **Free trial** — the form creates the account (no card) and sends a
  confirmation link.
- **Buy now** — the form sends the visitor to Stripe Checkout; the account is
  created only when Stripe confirms the payment, then the confirmation link
  is sent.
- Clicking the link (`verified.html`) confirms the address, and the app emails
  the login details: the email address and a generated password. The first
  sign-in asks them to choose their own password.

## Preview locally

    python3 -m http.server 8080    # then open http://localhost:8080
