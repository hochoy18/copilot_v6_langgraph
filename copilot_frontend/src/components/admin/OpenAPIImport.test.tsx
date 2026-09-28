import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { OpenAPIImport } from '@/components/admin/OpenAPIImport'
import type { Tool } from '@/types/tool'
import type { ToolDraft } from '@/types/openapi-import'

/**
 * Tests for the OpenAPI import preview — T15 / #13 + T16 / #14.
 *
 * Acceptance criteria from the issue bodies:
 * - [ ] 上传文件解析显示
 * - [ ] 粘贴 URL 解析显示
 * - [ ] 预览列出每个 draft
 * - [ ] 可逐个激活 / 弃用
 * - [ ] (T16) 管理员可 review:LLM 改写后的描述可编辑、原文可对照、
 *   导入级 warning 可见
 *
 * `mockFetch` arms `globalThis.fetch` with a sequence of queued
 * responses. The first call in each test is the import preview
 * (POST /admin/tools/import/openapi); activate flows follow with
 * one POST /admin/tools plus one PATCH /admin/tools/{id}. Each
 * test asserts against the recorded URL / body / method to lock
 * down the wire contract.
 *
 * File uploads are exercised via `File` + `FileReader`-style
 * reads — `URL` + `Blob` + `File` are available in jsdom, so the
 * component's `await file.text()` runs end-to-end without mocking.
 */

function makeDraft(overrides: Partial<ToolDraft> = {}): ToolDraft {
  return {
    operation_ref: 'GET /pets',
    name: 'listPets',
    description: 'List all pets.',
    original_description: null,
    description_generated: false,
    risk_level: 'read',
    status: 'draft',
    parameters_schema: { type: 'object', properties: {}, required: [] },
    http_method: 'GET',
    http_url_template: 'https://api.example.com/pets',
    http_headers: { Accept: 'application/json' },
    http_body_template: null,
    source: 'openapi',
    source_ref: 'get /pets',
    credentials_ref: null,
    warnings: [],
    ...overrides,
  }
}

function makePreviewResponse(drafts: ToolDraft[], overrides: Partial<{
  title: string | null
  version: string | null
  server_url: string | null
  source_format: 'json' | 'yaml'
  warnings: string[]
}> = {}) {
  return {
    drafts,
    title: 'Petstore',
    version: '1.0.0',
    server_url: 'https://api.example.com',
    source_format: 'json' as const,
    warnings: [] as string[],
    ...overrides,
  }
}

function makeCreatedTool(overrides: Partial<Tool> = {}): Tool {
  return {
    id: 'tool-created-1',
    name: 'listPets',
    description: 'List all pets.',
    risk_level: 'read',
    status: 'draft',
    parameters_schema: {},
    http_method: 'GET',
    http_url_template: 'https://api.example.com/pets',
    http_headers: {},
    http_body_template: null,
    source: 'manual',
    source_ref: 'get /pets',
    credentials_ref: null,
    created_at: '2026-09-28T08:00:00Z',
    updated_at: '2026-09-28T08:00:00Z',
    ...overrides,
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Replace `globalThis.fetch` with a `vi.fn`. Each response is
 * consumed in order. The test inspects `fetchMock.mock.calls` for
 * the URL / method / body assertions.
 */
function mockFetch(responses: ReadonlyArray<Response>): ReturnType<typeof vi.fn> {
  const fn = vi.fn()
  for (const body of responses) fn.mockResolvedValueOnce(body)
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

beforeEach(() => {
  // jsdom doesn't ship URL.createObjectURL; some test paths don't
  // touch it but the File reader path could.
  if (!('createObjectURL' in URL)) {
    Object.defineProperty(URL, 'createObjectURL', {
      value: vi.fn(() => 'blob:mock'),
      configurable: true,
    })
  }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('OpenAPIImport', () => {
  it('renders file upload + URL paste tabs', () => {
    mockFetch([])
    render(<OpenAPIImport />)
    expect(screen.getByTestId('tab-file')).toBeInTheDocument()
    expect(screen.getByTestId('tab-url')).toBeInTheDocument()
    expect(screen.getByTestId('file-input')).toBeInTheDocument()
  })

  it('parses an uploaded JSON file and lists one row per draft', async () => {
    const drafts = [
      makeDraft({ operation_ref: 'GET /pets', name: 'listPets' }),
      makeDraft({ operation_ref: 'POST /pets', name: 'createPet', risk_level: 'write' }),
    ]
    const fetchMock = mockFetch([jsonResponse(makePreviewResponse(drafts))])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)

    expect(await screen.findByTestId('draft-row-GET /pets')).toBeInTheDocument()
    expect(screen.getByTestId('draft-row-POST /pets')).toBeInTheDocument()
    expect(screen.getByTestId('preview-header')).toHaveTextContent('Petstore')

    // Wire contract: POST /admin/tools/import/openapi with { spec: {...} }.
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/admin/tools/import/openapi')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toEqual({ spec: { openapi: '3.0.0' } })
  })

  it('parses an uploaded YAML file by sending spec_yaml', async () => {
    const fetchMock = mockFetch([jsonResponse(makePreviewResponse([makeDraft()]))])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const yaml = 'openapi: 3.0.0\ninfo:\n  title: x\n  version: "1"\npaths: {}\n'
    const file = new File([yaml], 'openapi.yaml', { type: 'application/yaml' })
    await user.upload(screen.getByTestId('file-input'), file)

    await screen.findByTestId('preview-panel')
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init?.body as string)).toEqual({ spec_yaml: yaml })
  })

  it('fetches a pasted URL and runs the parse on the response text', async () => {
    const fetchMock = mockFetch([
      new Response('{"openapi":"3.0.0","info":{"title":"Remote","version":"1"}}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
      jsonResponse(makePreviewResponse([makeDraft()], { title: 'Remote' })),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    await user.click(screen.getByTestId('tab-url'))
    await user.type(screen.getByTestId('url-input'), 'https://example.com/openapi.json')
    await user.click(screen.getByTestId('fetch-url'))

    expect(await screen.findByTestId('preview-panel')).toBeInTheDocument()
    // First call: the URL fetch itself.
    expect(fetchMock.mock.calls[0][0]).toBe('https://example.com/openapi.json')
    // Second call: the import POST with the inline JSON body.
    const [importUrl, importInit] = fetchMock.mock.calls[1]
    expect(importUrl).toBe('/api/v1/admin/tools/import/openapi')
    expect(importInit?.method).toBe('POST')
    expect(JSON.parse(importInit?.body as string)).toEqual({
      spec: { openapi: '3.0.0', info: { title: 'Remote', version: '1' } },
    })
  })

  it('surfaces a parse error when the backend returns 400', async () => {
    mockFetch([
      jsonResponse(
        { code: 'openapi_parse_error', message_en: 'Unsupported OpenAPI version' },
        400,
      ),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"2.0"}'], 'old.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)

    const alert = await screen.findByTestId('parse-error')
    expect(alert).toHaveTextContent('400')
    expect(alert).toHaveTextContent('Unsupported OpenAPI version')
    expect(screen.queryByTestId('preview-panel')).not.toBeInTheDocument()
  })

  it('surfaces a network error when the URL fetch itself fails', async () => {
    mockFetch([
      new Response('Not Found', { status: 404, statusText: 'Not Found' }),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)
    await user.click(screen.getByTestId('tab-url'))
    await user.type(screen.getByTestId('url-input'), 'https://example.com/missing.json')
    await user.click(screen.getByTestId('fetch-url'))

    const alert = await screen.findByTestId('parse-error')
    expect(alert).toHaveTextContent('404')
  })

  it('activates a draft via POST /admin/tools + PATCH {status:active}', async () => {
    const fetchMock = mockFetch([
      jsonResponse(makePreviewResponse([makeDraft()])),
      jsonResponse(makeCreatedTool()),
      jsonResponse(makeCreatedTool({ status: 'active' })),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    await screen.findByTestId('draft-row-GET /pets')
    await user.click(screen.getByTestId('draft-activate-GET /pets'))

    await waitFor(() => {
      expect(screen.getByTestId('draft-status-active')).toHaveTextContent('tool-created-1')
    })
    // Call 1 = import; Call 2 = POST /admin/tools; Call 3 = PATCH /admin/tools/{id}
    expect(fetchMock.mock.calls).toHaveLength(3)
    const [createUrl, createInit] = fetchMock.mock.calls[1]
    expect(createUrl).toBe('/api/v1/admin/tools')
    expect(createInit?.method).toBe('POST')
    expect(JSON.parse(createInit?.body as string)).toMatchObject({
      name: 'listPets',
      risk_level: 'read',
      http_method: 'GET',
      http_url_template: 'https://api.example.com/pets',
      // ADR-0003 §21 — provenance must travel with the activated row.
      source: 'openapi',
      source_ref: 'get /pets',
    })
    const [patchUrl, patchInit] = fetchMock.mock.calls[2]
    expect(patchUrl).toBe('/api/v1/admin/tools/tool-created-1')
    expect(patchInit?.method).toBe('PATCH')
    expect(JSON.parse(patchInit?.body as string)).toEqual({ status: 'active' })
  })

  it('surfaces an activate error and keeps the draft row in error state', async () => {
    mockFetch([
      jsonResponse(makePreviewResponse([makeDraft()])),
      jsonResponse({ code: 'duplicate_key', message_en: 'name exists' }, 409),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    await screen.findByTestId('draft-row-GET /pets')
    await user.click(screen.getByTestId('draft-activate-GET /pets'))

    const err = await screen.findByTestId('draft-status-error')
    expect(err).toHaveTextContent('name exists')
    // Activate button re-enabled so the admin can retry after renaming.
    expect(
      (screen.getByTestId('draft-activate-GET /pets') as HTMLButtonElement).disabled,
    ).toBe(false)
  })

  it('discards a draft and removes its row from the preview', async () => {
    mockFetch([
      jsonResponse(
        makePreviewResponse([
          makeDraft({ operation_ref: 'GET /pets', name: 'listPets' }),
          makeDraft({ operation_ref: 'POST /pets', name: 'createPet' }),
        ]),
      ),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    await screen.findByTestId('draft-row-POST /pets')
    await user.click(screen.getByTestId('draft-discard-GET /pets'))

    await waitFor(() => {
      expect(screen.queryByTestId('draft-row-GET /pets')).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('draft-row-POST /pets')).toBeInTheDocument()
    expect(screen.getByTestId('draft-count')).toHaveTextContent('共 1 个 draft')
  })

  it('lets the admin edit the name and risk_level before activating', async () => {
    mockFetch([
      jsonResponse(makePreviewResponse([makeDraft({ name: 'listPets', risk_level: 'read' })])),
      jsonResponse(makeCreatedTool({ name: 'list_pets_v2', risk_level: 'destructive' })),
      jsonResponse(makeCreatedTool({ name: 'list_pets_v2', risk_level: 'destructive', status: 'active' })),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    await screen.findByTestId('draft-row-GET /pets')

    const nameInput = screen.getByTestId('draft-name-GET /pets') as HTMLInputElement
    await user.clear(nameInput)
    await user.type(nameInput, 'list_pets_v2')
    await user.selectOptions(screen.getByTestId('draft-risk-GET /pets'), 'destructive')

    await user.click(screen.getByTestId('draft-activate-GET /pets'))

    await waitFor(() => {
      expect(screen.getByTestId('draft-status-active')).toBeInTheDocument()
    })
    const [, createInit] = (globalThis.fetch as unknown as { mock: { calls: Array<[string, RequestInit | undefined]> } }).mock.calls[1]
    expect(JSON.parse(createInit?.body as string)).toMatchObject({
      name: 'list_pets_v2',
      risk_level: 'destructive',
    })
  })

  it('renders per-draft warnings when the parser surfaces them', async () => {
    mockFetch([
      jsonResponse(
        makePreviewResponse([
          makeDraft({
            operation_ref: 'POST /uploads',
            warnings: [
              'Operation is missing `operationId`; slug was synthesised from method + path.',
              "Request body media type ['multipart/form-data'] is not JSON; schema not derived — register this Tool manually.",
            ],
          }),
        ]),
      ),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    const row = await screen.findByTestId('draft-row-POST /uploads')
    const warnings = within(row).getByTestId('draft-warnings-POST /uploads')
    expect(warnings).toHaveTextContent('missing `operationId`')
    expect(warnings).toHaveTextContent('multipart/form-data')
  })

  it('shows the LLM rewrite badge and keeps the original text for comparison', async () => {
    mockFetch([
      jsonResponse(
        makePreviewResponse([
          makeDraft({
            description: '根据编号查询宠物资料\n\n典型用例:\n- 查一下 7 号宠物的信息',
            original_description: 'Returns a user by ID. See Swagger section 4.',
            description_generated: true,
          }),
        ]),
      ),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    const row = await screen.findByTestId('draft-row-GET /pets')

    expect(
      within(row).getByTestId('draft-description-generated-GET /pets'),
    ).toHaveTextContent('LLM 改写')
    const textarea = within(row).getByTestId(
      'draft-description-GET /pets',
    ) as HTMLTextAreaElement
    expect(textarea.value).toContain('根据编号查询宠物资料')
    expect(
      within(row).getByTestId('draft-original-description-GET /pets'),
    ).toHaveTextContent('Returns a user by ID. See Swagger section 4.')
  })

  it('sends the admin-edited description when activating', async () => {
    const fetchMock = mockFetch([
      jsonResponse(makePreviewResponse([makeDraft()])),
      jsonResponse(makeCreatedTool()),
      jsonResponse(makeCreatedTool({ status: 'active' })),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    const row = await screen.findByTestId('draft-row-GET /pets')

    const textarea = within(row).getByTestId(
      'draft-description-GET /pets',
    ) as HTMLTextAreaElement
    await user.clear(textarea)
    await user.type(textarea, '查询宠物详细信息')

    await user.click(screen.getByTestId('draft-activate-GET /pets'))
    await waitFor(() => expect(screen.getByTestId('draft-status-active')).toBeInTheDocument())

    const [, createInit] = fetchMock.mock.calls[1]
    expect(JSON.parse(createInit?.body as string)).toMatchObject({
      description: '查询宠物详细信息',
    })
  })

  it('renders import-level warnings above the preview', async () => {
    mockFetch([
      jsonResponse(
        makePreviewResponse([makeDraft()], {
          warnings: [
            'LLM description generation is not configured; drafts keep their raw OpenAPI descriptions.',
          ],
        }),
      ),
    ])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)

    const banner = await screen.findByTestId('import-warnings')
    expect(banner).toHaveTextContent('LLM description generation is not configured')
  })

  it('resets the preview when the admin hits 重新导入', async () => {
    mockFetch([jsonResponse(makePreviewResponse([makeDraft()]))])
    const user = userEvent.setup()
    render(<OpenAPIImport />)

    const file = new File(['{"openapi":"3.0.0"}'], 'openapi.json', { type: 'application/json' })
    await user.upload(screen.getByTestId('file-input'), file)
    await screen.findByTestId('preview-panel')
    await user.click(screen.getByTestId('reset-preview'))
    await waitFor(() => {
      expect(screen.queryByTestId('preview-panel')).not.toBeInTheDocument()
    })
  })

  })
