---
name: djarum-common-mistake-guidelines
description: Reference guidelines for the Djarum Common Mistake (CM) design-review pipeline - design-type classification, brand identification, KV/branding review, marketing review, bounding-box annotation, best-KV recommendation, and follow-up query handling.
metadata:
  version: 0.1.1
---

# Djarum Common Mistake Guidelines

This skill is the source of truth for review criteria and stage order.
If this skill conflicts with the agent instruction, this skill wins for those two things.
The agent instruction wins for routing, tool usage, chat output formatting, **which reference file each stage reads**, and every fallback that ends a turn.
That includes how many images one turn may carry: stop wherever the instruction says to, even partway through the stages below.

One skill serves every Djarum CM deployment, so it holds every category's prompts and picks none of them.
Where a stage below says the instruction names the path, read the path your instruction names and no other.
Never substitute another category's file, and never ask the user which category to use.

## How to use this skill

This skill packages the Djarum CM prompt catalog as one reference file per catalog entry.
Read one reference at a time so only that stage's criteria enter context.

Before running a pipeline stage, `skill_resource` the exact reference listed for that stage.
Never inspect the image until that `skill_resource` call has returned.
If the stage needs to look at an image, then `read_image` with `query` set to that reference's full prompt: the **System prompt** and **User prompt template**, copied verbatim.
The only changes to make are to replace every `{placeholder}` in that prompt, e.g., `{kv_design_review_context}` or `{logo_phw_size_summary}`, with its actual value for this turn and this image, and to append any context block the agent instruction names for that stage.
Never send a query that still contains a `{placeholder}`.
Do not paraphrase a stage's criteria from memory or from a previous turn.
Do not invent a shorter `read_image` query.
Re-read the reference file with `skill_resource` each time you execute that stage, since thresholds and decision rules are exact and versioned.
Never invent or soften a numeric standard (e.g., logo/PHW size thresholds) - use the exact figure from the reference file for that stage.

A folder holding more than one file is a folder whose file the instruction chooses.
Where a task has a Djarum and a brand-agnostic body, the two are named `_djarum_kv_brand.md` and `_mixed_kv_brand.md` so neither reads as the default.
Those pairs and the six `answer_design/` files are never selected here.

## When to use

The agent instruction routes the user message to one of these pipelines.
Then follow the matching section below.

| Routed pipeline | Start at |
|---|---|
| Full KV review | Stage 1 |
| Full KV review, then best-KV recommendation | Stages 1-4 for each image, then Stage 7 |
| Marketing review (no compliance review) | Stage 1, skipping Stages 3, 4, and 5 |
| Brand identification only | Stage 2 |
| Follow-up query | Stage 8 |

## Full KV review

Run these stages in order for each uploaded image.
Never skip Stage 1, even if the user's message strongly implies the image is a KV.

1. **Design-type classification** - the path your instruction names. Classify the image.
   If the result is not `Key Visual Design`, stop the pipeline.
   Tell the user this agent only reviews Key Visuals, explain why the image was classified as `Packaging Design` or `__default__`, and do not run the remaining stages.
2. **Brand and variant identification** - the path your instruction names.
   Use this to personalize later stages (e.g., which brand's tagline/color rules apply).
   Never fabricate a brand, variant, or campaign detail that this stage did not identify from the image.
   If brand identification is inconclusive (`detected` is `false` or `null`), stop the pipeline and say so instead of guessing: the agent instruction owns what to tell the user.
3. **`references/get_logo_and_phw_bounding_box/get_logo_and_phw_bounding_box.md`** - locate the logo and PHW and compute their size relative to the canvas.
   Keep this size summary for Stage 4.
   Do not show raw bounding-box coordinates unless the user asks for them.
4. **KV compliance review** - the `references/answer_design/` path your instruction names. Produce the **KV Review** section (Main Hero, Logo Size, QR Code, Tagline, PHW), using the logo/PHW size summary from Stage 3.
   Where a review prompt names a retrieved criteria or brand-data value, e.g., the category's minimum logo size or the brand's tagline, that value comes from `knowledge_retrieval_tool` and from nowhere else.
   Skip this stage, and Stage 5 with it, when your instruction names no review path.
5. **`references/detect_failed_elements/detect_failed_elements_bounding_box.md`** - for every ❓ item in the Stage 4 output, locate and box it on the image.
   Your instruction names the tool that draws these boxes onto the image and how to show the result: that annotated image is this stage's only output.
   Only include elements that were actually flagged ❓.
6. **Marketing review** - the path your instruction names. Produce the **Marketing Review** section (subjective critique above the PHW area: brand identity, ideas, layout, talent/props, image quality, message clarity, appeal, compliance).

## Best-KV recommendation

7. Once every uploaded image has been through its own stages, `skill_resource` **`references/answer_recommend_best_kv_design/answer_recommend_best_kv_design.md`**.
   Where the pipeline writes a KV Review, those stages are Stages 1-4: a multi-KV turn skips Stages 5 and 6, so it neither boxes failed elements nor writes a Marketing Review for any KV.
   Where the pipeline writes a marketing review only, they are Stages 1, 2, and 6.
   Use the individual reviews to recommend the strongest KV overall.
   That reference owns the mixed-variant warning and the recommendation format.

## Follow-up query

8. `skill_resource` **`references/identify_follow_up_query_intention/identify_follow_up_query_intention.md`** first to classify the intention:
   - **`basic`** - `skill_resource` **`references/answer_follow_up_query/answer_follow_up_query.md`** and answer from the conversation history.
   - **`image_generation`** - `skill_resource` **`references/get_image_generation_config/get_image_generation_config.md`** and compute the image-generation payload it describes.

   Stage 8 works from the conversation, not the image, so none of its references goes through `read_image`: follow each one's **System prompt** yourself.

Three stages hand their output to a tool once the vision pass has returned it, and the agent instruction owns every handover.
Logo and PHW sizing is measured by `logo_phw_size_estimator_tool`, so report the sizes it returns rather than an estimate read off the image.
Failed elements are drawn by `draw_annotator_tool`, which emits the annotated key visual as an image.
The `image_generation` follow-up's payload is generated by `image_generator_tool`, which emits the result as an image; a status other than `generated` means describe the intended image in text instead, never claim to have generated or edited one.

The criteria and brand data these stages review against are retrieved by `knowledge_retrieval_tool` before Stage 4, and the agent instruction owns that call too.
No threshold in this skill is a substitute for a retrieved one: where the two differ, the retrieved value is the one that decides.

## Prompt catalog index

Every valid reference path is listed here.
A path not in this table does not exist: if an instruction names one, say so rather than guessing at a near match.
Where a stage has more than one row, the agent instruction has already chosen which of them this deployment uses.

| Catalog ID | Reference file | Stage |
|---|---|---|
| `djarum_cm_identify_design_type` | `references/identify_design_type/identify_design_type_djarum_kv_brand.md` | Stage 1 |
| `djarum_cm_identify_design_type` (mixed-KV-brand) | `references/identify_design_type/identify_design_type_mixed_kv_brand.md` | Stage 1 |
| `djarum_cm_identify_brand_name` | `references/identify_brand_name/identify_brand_name_djarum_kv_brand.md` | Stage 2 |
| `djarum_cm_identify_brand_name` (mixed-KV-brand) | `references/identify_brand_name/identify_brand_name_mixed_kv_brand.md` | Stage 2 |
| `djarum_cm_get_logo_and_phw_bounding_box` | `references/get_logo_and_phw_bounding_box/get_logo_and_phw_bounding_box.md` | Stage 3 |
| `djarum_cm_answer_branding_design` | `references/answer_design/branding.md` | Stage 4 |
| `djarum_cm_answer_thematic_design` | `references/answer_design/thematic.md` | Stage 4 |
| `djarum_cm_answer_tactical_price_design` | `references/answer_design/tactical_price.md` | Stage 4 |
| `djarum_cm_answer_tactical_quality_campaign_design` | `references/answer_design/tactical_quality_campaign.md` | Stage 4 |
| `djarum_cm_answer_new_product_launching_design` | `references/answer_design/new_product_launching.md` | Stage 4 |
| `djarum_cm_answer_limited_edition_design` | `references/answer_design/limited_edition.md` | Stage 4 |
| `djarum_cm_detect_failed_elements_bounding_box` | `references/detect_failed_elements/detect_failed_elements_bounding_box.md` | Stage 5 |
| `djarum_cm_answer_marketing_design` | `references/answer_marketing_design/answer_marketing_design_djarum_kv_brand.md` | Stage 6 |
| `djarum_cm_answer_marketing_design` (mixed-KV-brand) | `references/answer_marketing_design/answer_marketing_design_mixed_kv_brand.md` | Stage 6 |
| `djarum_cm_answer_recommend_best_kv_design` | `references/answer_recommend_best_kv_design/answer_recommend_best_kv_design.md` | Stage 7 |
| `djarum_cm_identify_follow_up_query_intention` | `references/identify_follow_up_query_intention/identify_follow_up_query_intention.md` | Stage 8 |
| `djarum_cm_answer_follow_up_query` | `references/answer_follow_up_query/answer_follow_up_query.md` | Stage 8 (`basic`) |
| `djarum_cm_get_image_generation_config` | `references/get_image_generation_config/get_image_generation_config.md` | Stage 8 (`image_generation`) |

The source of truth for these prompts is the Djarum CM prompt catalog owned by the Djarum/AIE team.
The `references/` files are a 1:1 extraction of those catalogs, one file per catalog entry (and one extra file per mixed-KV-brand body that differs), at prompt version `0.116`.
If the catalog is updated, regenerate the corresponding `references/` file(s) rather than editing the reference files by hand.

## Reference file format

Each `references/**/*.md` file has:

- **Prompt catalog ID, active flag, and prompt version** - identifies the exact catalog row.
- **Why this exists** - the product rationale from the Djarum/AIE team.
- **System prompt** and **User prompt template** - verbatim from the catalog, unmodified.
  Their `{placeholder}` slots are filled in at call time, never in the file.
- **Notes** - AIE/DD/general notes from the catalog, when present (e.g., a DD note recording a requirement change).
