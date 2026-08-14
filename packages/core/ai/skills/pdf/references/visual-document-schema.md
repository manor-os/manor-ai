# Visual Document Composition

Use this mode for image-led briefs, listings, portfolios, catalogs, field guides, case studies, and other documents whose page structure must adapt to the supplied images and copy. It is a semantic block composer, not a fixed visual template.

## Routing

Choose `compose_visual_document.py` when the request has two or more of these traits:

- multiple user-provided, downloaded, or generated images;
- repeated records such as properties, products, projects, locations, or cases;
- native tables, captions, status notes, contacts, or supporting copy;
- content-dependent page count or image-grid selection;
- a request to remix the same content with different assets.

Choose a bundled fixed template only when the user explicitly requests one or the content closely matches a business report, project proposal, invoice, or academic paper.

## Recipe

```json
{
  "title": "Berkeley Partner Housing Listings",
  "subtitle": "A concise partner inventory and follow-up brief",
  "brand": "Example Housing",
  "author": "Operations Team",
  "page_size": "LETTER",
  "meta": ["Updated: 2026-08-10", "Audience: partner operations"],
  "footer": "Example Housing · Internal",
  "theme": {
    "primary": "245783",
    "accent": "C7931D"
  },
  "sections": [
    {
      "title": "Property One",
      "subtitle": "South Berkeley · walkable to campus",
      "blocks": [
        {
          "type": "key_value_table",
          "rows": [
            {"label": "Layout", "value": "2 bed / 2 bath"},
            {"label": "Rent", "value": "$3,995 / month"},
            {
              "label": "Listing",
              "value": "Open listing",
              "link": "https://example.com/listing"
            }
          ]
        },
        {
          "type": "callout",
          "tone": "success",
          "text": "Best current match; partnership intent confirmed."
        },
        {
          "type": "image",
          "path": "assets/property-one-exterior.jpg",
          "caption": "Property exterior",
          "height": 88,
          "fit": "cover",
          "focal_x": 0.5,
          "focal_y": 0.45
        },
        {
          "type": "gallery",
          "columns": 2,
          "aspect_ratio": 1.5,
          "items": [
            {"path": "assets/living-room.jpg", "caption": "Living room"},
            {"path": "assets/kitchen.jpg", "caption": "Kitchen"},
            {"path": "assets/bedroom-one.jpg", "caption": "Bedroom one"},
            {"path": "assets/bedroom-two.jpg", "caption": "Bedroom two"}
          ]
        }
      ]
    },
    {
      "title": "Partners in follow-up",
      "blocks": [
        {
          "type": "contact_list",
          "items": [
            {
              "name": "Oxford Property Management",
              "details": "Ada Ormsby · ada@example.com · 510-555-0123",
              "note": "No current availability; follow up in December."
            }
          ]
        }
      ]
    }
  ]
}
```

## Blocks

- `paragraph`: `text`, optional `heading`.
- `key_value_table`: non-empty `rows`; each row has `label`, `value`, and optional external `link` beginning with `http://`, `https://`, `mailto:`, or `tel:`.
- `callout`: `text`, optional `title`; `tone` is `success`, `warning`, `info`, or `surface`.
- `image`: local `path`, optional `caption`, `height`, `fit`, `focal_x`, and `focal_y`.
- `gallery`: non-empty `items`; optional `columns`, `aspect_ratio`, and `gap`. Each item accepts the image fields above. The composer groups at most six images per page region.
- `contact_list`: non-empty `items`; each item has `name` and optional `details` and `note`.
- `spacer`: optional `height` from 0 to 30 mm. Use sparingly.

Paths are resolved relative to the recipe file. Remote image URLs are rejected; prepare and record local image assets first.

## Layout Contract

- Keep a section heading with its first content block.
- Keep image galleries together when they fit; start a new page rather than clipping them.
- Repeat the current section name on continuation pages.
- Preserve image aspect ratio through focal-point cropping or contained placement.
- Draw titles, labels, tables, and captions as native PDF text; never bake factual text into generated images.
- Reject unsupported block types, missing assets, invalid links, and unknown theme colors before delivery.
- Always run `verify_pdf.py` and inspect every rendered page after composition.

## Command

```bash
python scripts/compose_visual_document.py \
  tmp/pdfs/visual-document.json \
  output/pdf/visual-document.pdf
python scripts/verify_pdf.py \
  output/pdf/visual-document.pdf \
  tmp/pdfs/visual-document-review
```
