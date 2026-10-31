from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job

from .artwork.storage import cleanup


@system_job(interval=JobIntervalChoices.INTERVAL_DAILY)
class ArtworkCleanupJob(JobRunner):
    """Remove artwork blobs that no revision uses."""

    class Meta:
        name = "Artwork blob cleanup"

    def run(self, *args, **kwargs):
        self.logger.info(f"Removed {cleanup()} unused artwork blobs.")
