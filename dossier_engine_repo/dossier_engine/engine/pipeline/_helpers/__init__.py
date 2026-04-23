"""
Cross-phase helpers used by multiple pipeline phases.

Not phases themselves — just utilities that got grouped here during
Round 34 to reduce crowding of the flat pipeline/ directory.

Contents:
    eligibility.py — compute_eligible_activities, filter_by_user_auth,
                     derive_allowed_activities
    status.py      — derive_status (shared by authorization, eligibility,
                     finalization, response)
    invariants.py  — enforce_used_generated_disjoint (called multiple times)
    identity.py    — resolve_handler_generated_identity (entity-identity
                     resolution for handler-generated entities). Formerly
                     _identity.py at the pipeline root; underscore dropped
                     because the _helpers/ name already signals privacy.
"""
