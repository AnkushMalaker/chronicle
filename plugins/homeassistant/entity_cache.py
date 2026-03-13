"""
Entity cache for Home Assistant integration.

This module provides caching and lookup functionality for Home Assistant areas and entities.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EntityCache:
    """Cache for Home Assistant areas and entities."""

    areas: List[str] = field(default_factory=list)
    """List of area names (e.g., ["study", "living_room"])"""

    area_entities: Dict[str, List[str]] = field(default_factory=dict)
    """Map of area names to entity IDs (e.g., {"study": ["light.tubelight_3"]})"""

    entity_details: Dict[str, Dict] = field(default_factory=dict)
    """Full entity state data keyed by entity_id"""

    label_areas: Dict[str, List[str]] = field(default_factory=dict)
    """Map of label names to area names (e.g., {"hall": ["dining_room", "living_room"]})"""

    last_refresh: datetime = field(default_factory=datetime.now)
    """Timestamp of last cache refresh"""

    def find_entity_by_name(self, name: str) -> Optional[str]:
        """
        Find entity ID by fuzzy name matching.

        Matching priority:
        1. Exact friendly_name match (case-insensitive)
        2. Partial friendly_name match (case-insensitive)
        3. Entity ID match (e.g., "tubelight_3" → "light.tubelight_3")

        Args:
            name: Entity name to search for

        Returns:
            Entity ID if found, None otherwise
        """
        name_lower = name.lower().strip()

        # Step 1: Exact friendly_name match
        for entity_id, details in self.entity_details.items():
            friendly_name = details.get("attributes", {}).get("friendly_name", "")
            if friendly_name.lower() == name_lower:
                logger.debug(
                    f"Exact match: {name} → {entity_id} (friendly_name: {friendly_name})"
                )
                return entity_id

        # Step 2: Partial friendly_name match
        for entity_id, details in self.entity_details.items():
            friendly_name = details.get("attributes", {}).get("friendly_name", "")
            if name_lower in friendly_name.lower():
                logger.debug(
                    f"Partial match: {name} → {entity_id} (friendly_name: {friendly_name})"
                )
                return entity_id

        # Step 3: Entity ID match (try adding common domains)
        common_domains = ["light", "switch", "fan", "cover"]
        for domain in common_domains:
            candidate_id = f"{domain}.{name_lower.replace(' ', '_')}"
            if candidate_id in self.entity_details:
                logger.debug(f"Entity ID match: {name} → {candidate_id}")
                return candidate_id

        logger.warning(f"No entity found matching: {name}")
        return None

    def get_entities_in_area(
        self, area: str, entity_type: Optional[str] = None
    ) -> List[str]:
        """
        Get all entities in an area, optionally filtered by domain.

        Args:
            area: Area name (case-insensitive)
            entity_type: Entity domain filter (e.g., "light", "switch")

        Returns:
            List of entity IDs in the area
        """
        area_lower = area.lower().strip()

        # Find matching area (case-insensitive)
        matching_area = None
        for area_name in self.areas:
            if area_name.lower() == area_lower:
                matching_area = area_name
                break

        if matching_area:
            entities = self.area_entities.get(matching_area, [])
        else:
            # Fallback: check if it's a label that maps to multiple areas
            matching_label = None
            for label_name in self.label_areas:
                if label_name.lower() == area_lower:
                    matching_label = label_name
                    break

            if not matching_label:
                logger.warning(f"Area not found: {area}")
                return []

            # Resolve label → areas → entities
            real_areas = self.label_areas[matching_label]
            logger.info(f"Resolved label '{area}' → areas {real_areas}")
            entities = []
            for real_area in real_areas:
                entities.extend(self.area_entities.get(real_area, []))

        # Filter by entity type if specified
        if entity_type:
            entity_type_lower = entity_type.lower()
            entities = [e for e in entities if e.split(".")[0] == entity_type_lower]

        logger.debug(
            f"Found {len(entities)} entities in area '{area}'"
            + (f" (type: {entity_type})" if entity_type else "")
        )

        return entities

    def get_cache_age_seconds(self) -> float:
        """Get cache age in seconds."""
        return (datetime.now() - self.last_refresh).total_seconds()

    def is_stale(self, max_age_seconds: int = 3600) -> bool:
        """
        Check if cache is stale.

        Args:
            max_age_seconds: Maximum cache age before considering stale (default: 1 hour)

        Returns:
            True if cache is older than max_age_seconds
        """
        return self.get_cache_age_seconds() > max_age_seconds
