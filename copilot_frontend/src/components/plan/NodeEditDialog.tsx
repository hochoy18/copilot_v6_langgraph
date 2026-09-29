/**
 * Node parameter-edit dialog — T27 / #23, ADR-0019, ADR-0029.
 *
 * The T27 acceptance criterion in three lines:
 *
 *   点节点弹表单 ─ clicking a Plan node opens this dialog against it.
 *   改 hello→world ─ the user edits parameters in the JSON textarea.
 *   批准后 Tool 用新参数 / 返回 world ─ the post-PATCH Plan has
 *                                    `status="modified"` and the new
 *                                    parameters, so the Worker's
 *                                    downstream Tool call uses them.
 *
 * Built on the shadcn/ui Dialog primitive (`components/ui/dialog.tsx`,
 * ADR-0029: "UI 组件: shadcn/ui(Tailwind + Radix UI)"). Radix gives us
 * focus trap, ESC handling, backdrop dismissal, scroll lock, and
 * portal-to-body stacking — none of which the hand-rolled first cut
 * (T27 review hard finding) could provide without re-implementing
 * Radix by hand. The Radix `Dialog.Root` `open` prop is the single
 * source of truth for visibility; `onOpenChange(false)` is how we
 * surface "user dismissed" back to the parent.
 *
 * Form state lives in this component (two strings — JSON-encoded
 * `parameters` + plain-text `notes`) so the dialog's render path
 * stays decoupled from the drawer store; `PlanDrawer` wires
 * `open` / `onClose` / `onSaved` against `usePlanDrawerStore`. On a
 * successful save the parent folds the returned Plan into the store
 * via `replacePlan`, the same single write point (approve / reject /
 * `markExecutionOutcome` share per issue #53's handoff.
 *
 * Validation is intentionally local-first:
 * - parameters must parse as JSON
 * - parameters must be an object (the backend's `record_edit`
 *   repository contract requires every `node_id` + `tool` to match
 *   the persisted Plan verbatim; sending a partial node list trips
 *   the `set-equality` check and surfaces as 400
 *   `validation_error`. The frontend therefore ships the *full*
 *   `nodes` list, with the edited node carrying the new parameters
 *   and un-edited nodes passing through unchanged.)
 *
 * Errors render inline below the form. Transport failures
 * (`plan_not_pending`, etc.) surface as the backend's `message_zh`
 * via `formatPlanDecisionError` — the same envelope contract as
 * `approvePlan` / `rejectPlan`.
 */
import { Loader2 } from 'lucide-react'
import { useState } from 'react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { editPlan, formatPlanDecisionError } from '@/lib/conversations-api'
import type { Plan, PlanNode } from '@/types/plan'

interface NodeEditDialogProps {
  plan: Plan
  nodeId: string
  open: boolean
  onClose(): void
  /**
   * Called with the backend's `status="modified"` Plan on a
   * successful save. The parent folds it into the drawer store
   * (the `replacePlan` action).
   */
  onSaved(plan: Plan): void
}

/**
 * Two-pane JSON string + plain-text state. We keep the parameters
 * textarea as a *string* (not parsed) so the user can type
 * mid-flight and we only validate at submit-time, where a parse
 * error renders inline. Storing parsed would lose intermediate
 * invalid states.
 */
interface FormState {
  parametersText: string
  notesText: string
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function getNode(plan: Plan, nodeId: string): PlanNode | undefined {
  return plan.nodes.find((n) => n.node_id === nodeId)
}

function initFormState(plan: Plan, nodeId: string): FormState | null {
  const node = getNode(plan, nodeId)
  if (!node) return null
  return {
    parametersText: formatJson(node.parameters),
    notesText: node.notes,
  }
}

/**
 * Compose the *full* edited node list for the PATCH body.
 *
 * The T26 repository contract (`PlanRepository.record_edit`) requires
 * every `node_id` + `tool` to match the persisted Plan exactly; the
 * backend's `set-equality` check rejects partial lists with
 * `validation_error`. We therefore ship every node, replacing the
 * targeted one with the new `parameters` + `notes` and leaving the
 * siblings untouched.
 */
function buildEditedNodes(
  plan: Plan,
  nodeId: string,
  parameters: Record<string, unknown>,
  notes: string,
): PlanNode[] | null {
  const target = getNode(plan, nodeId)
  if (!target) return null
  return plan.nodes.map((node) =>
    node.node_id === nodeId
      ? { ...node, parameters, notes }
      : node,
  )
}

export function NodeEditDialog({
  plan,
  nodeId,
  open,
  onClose,
  onSaved,
}: NodeEditDialogProps): React.ReactElement | null {
  // `key={editingNodeId}` on the wrapper in `PlanDrawer` re-mounts
  // this whole component when the user picks a different node, so
  // the `useState` initializers below always pick up the fresh
  // parameters / notes without a manual effect to re-hydrate.
  const initial = initFormState(plan, nodeId)
  const [parametersText, setParametersText] = useState(
    initial?.parametersText ?? '',
  )
  const [notesText, setNotesText] = useState(initial?.notesText ?? '')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (!initial) return null

  async function handleSubmit(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    setError(null)

    let parsed: unknown
    try {
      parsed = JSON.parse(parametersText)
    } catch {
      setError('参数必须是合法 JSON, 请检查格式。')
      return
    }
    if (
      parsed === null ||
      typeof parsed !== 'object' ||
      Array.isArray(parsed)
    ) {
      setError('参数必须是 JSON 对象 (例如 { "text": "hello" })。')
      return
    }

    const editedNodes = buildEditedNodes(
      plan,
      nodeId,
      parsed as Record<string, unknown>,
      notesText,
    )
    if (!editedNodes) {
      setError('节点已不存在, 请刷新 Plan 后重试。')
      return
    }

    setSubmitting(true)
    try {
      const updated = await editPlan(plan.conversation_id, editedNodes)
      onSaved(updated)
      onClose()
    } catch (err) {
      setError(formatPlanDecisionError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose()
      }}
    >
      <DialogContent data-testid="node-edit-dialog">
        <header className="space-y-1 pr-6">
          <DialogTitle>编辑节点参数</DialogTitle>
          <DialogDescription data-testid="node-edit-tool" className="font-mono">
            {getNode(plan, nodeId)?.tool ?? nodeId}
          </DialogDescription>
        </header>

        <form
          onSubmit={(e) => void handleSubmit(e)}
          className="flex flex-col gap-4"
        >
          <div className="space-y-2">
            <label
              htmlFor="node-edit-parameters"
              className="block text-xs font-medium text-muted-foreground"
            >
              参数 (JSON 对象)
            </label>
            <textarea
              id="node-edit-parameters"
              data-testid="node-edit-parameters"
              value={parametersText}
              onChange={(e) => setParametersText(e.target.value)}
              rows={8}
              spellCheck={false}
              className="w-full resize-y rounded-md border bg-background p-2 font-mono text-xs"
            />
          </div>

          <div className="space-y-2">
            <label
              htmlFor="node-edit-notes"
              className="block text-xs font-medium text-muted-foreground"
            >
              Planner 备注 (可选)
            </label>
            <textarea
              id="node-edit-notes"
              data-testid="node-edit-notes"
              value={notesText}
              onChange={(e) => setNotesText(e.target.value)}
              rows={2}
              className="w-full resize-y rounded-md border bg-background p-2 text-sm"
            />
          </div>

          {error && (
            <p
              data-testid="node-edit-error"
              role="alert"
              className="rounded-md bg-red-50 p-2 text-xs text-red-700"
            >
              {error}
            </p>
          )}

          <footer className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </Button>
            <Button
              type="submit"
              size="sm"
              data-testid="node-edit-submit"
              disabled={submitting}
            >
              {submitting ? (
                <>
                  <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                  保存中…
                </>
              ) : (
                '保存'
              )}
            </Button>
          </footer>
        </form>
      </DialogContent>
    </Dialog>
  )
}