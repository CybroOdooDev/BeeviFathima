# BioBridge marketing site

A static site: four HTML pages, one stylesheet, one small script. No build
step, no framework, no dependencies — upload the folder to any static host
(Netlify, Cloudflare Pages, GitHub Pages, S3, or an nginx `root`).

```
index.html      home
pricing.html    plans, what's included, FAQ
setup.html      public setup guide
contact.html    demo / sales request form
assets/
  styles.css    all styling (light + dark, responsive)
  site.js       app links, mobile menu, contact form
  favicon.svg
```

## Before you publish

1. **Point it at the app.** In `assets/site.js`, set `APP_URL` to where
   BioBridge runs (e.g. `https://app.biobridge.io`). Every "Sign in" and
   "Start free trial" link is filled from it (`/app/#/login`, `/app/#/signup`).
2. **Contact form.** A static site can't send mail on its own. Either set
   `FORM_ENDPOINT` to a form service that accepts JSON posts (Formspree,
   Basin, Getform, or your own endpoint), or leave it blank and the form opens
   the visitor's email app addressed to `SALES_EMAIL`.
3. **Fill the placeholders.** Search the HTML for `[` —
   `[COMPANY NAME]`, `[SALES EMAIL]`, `[PHONE NUMBER]`, `[OFFICE ADDRESS]`,
   `[RESPONSE TIME]`, `[SUPPORT LEVEL]`, `[SUPPORTED VERSIONS]`,
   `[TERMS URL]`, `[PRIVACY URL]`.
4. **Prices.** Pricing is written into `pricing.html` and the teaser on
   `index.html`, matching `tools/seed_plans.py` (Starter $49 / 25, Growth $149 /
   150, Scale $399 / unlimited). If you change plans in the app, change them
   here too — or ask for a version that reads them from the app's public
   `/plans` API.

## Preview locally

    python3 -m http.server 8080    # then open http://localhost:8080
