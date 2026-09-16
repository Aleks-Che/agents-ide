import type { PlanningJobView } from '../../api/planning'

export function confirmedPlans(
  jobs: PlanningJobView[],
  projectId: string,
  chatId: string,
) {
  return jobs.flatMap((job) => {
    if (
      job.project_id !== projectId ||
      (job.chat_id && job.chat_id !== chatId) ||
      job.state !== 'confirmed'
    )
      return []
    const revision = job.revisions?.at(-1)
    if (
      !revision?.confirmed_at ||
      !revision.confirmation_hash ||
      revision.readiness !== 'ready'
    )
      return []
    return [
      {
        job,
        revision,
        source: {
          job_id: job.id,
          revision_number: revision.revision_number,
          confirmation_hash: revision.confirmation_hash,
        },
      },
    ]
  })
}
