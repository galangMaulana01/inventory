# MongoDB + Cloudinary Follow-up Fix

## Critical migration bug fixed
The frontend originally converted product/color/variant IDs with `Number(...)`, while the MongoDB migration changed IDs to UUID strings. UUID strings became `NaN`, breaking detail, edit, transfer, and transaction flows. All ID routing/comparisons now preserve IDs as strings.

## Inventory consistency fixes
- Batch sale now validates the full request before stock mutation.
- Each stock decrement uses an atomic conditional MongoDB update.
- If a later batch item or database write fails, completed stock decrements and generated transaction/move records are compensated (rollback).
- Product-creation cleanup now also removes stock-move records if nested creation fails.

## Configuration
- Added `python-dotenv` and `load_dotenv()` for local development.
- MongoDB and Cloudinary credentials remain environment variables and are not hard-coded.

## Remaining production recommendation
For strongest multi-document consistency, deploy MongoDB as a replica set / Atlas cluster and move the batch sale and product creation flows to MongoDB sessions with `with_transaction()`. The current compensation logic is designed to work even where multi-document transactions are unavailable.
