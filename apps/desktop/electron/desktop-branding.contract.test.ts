import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'
import { test } from 'vitest'

import { DESKTOP_WINDOW_TITLE } from './desktop-branding'

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function source(relativePath: string) {
  return fs.readFileSync(path.join(desktopRoot, relativePath), 'utf8')
}

function propertyName(node: ts.PropertyName, sourceFile: ts.SourceFile) {
  if (ts.isIdentifier(node) || ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) {
    return node.text
  }

  return node.getText(sourceFile)
}

function objectPropertyInitializers(relativePath: string) {
  const contents = source(relativePath)
  const sourceFile = ts.createSourceFile(relativePath, contents, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const result = new Map<string, string>()

  function collect(node: ts.ObjectLiteralExpression, prefix: string[] = []) {
    for (const property of node.properties) {
      if (!ts.isPropertyAssignment(property)) {
        continue
      }

      const propertyPath = [...prefix, propertyName(property.name, sourceFile)]
      const initializer = property.initializer

      result.set(propertyPath.join('.'), initializer.getText(sourceFile))

      if (ts.isObjectLiteralExpression(initializer)) {
        collect(initializer, propertyPath)
      }
    }
  }

  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) {
      continue
    }

    for (const declaration of statement.declarationList.declarations) {
      const initializer = declaration.initializer

      if (initializer && ts.isObjectLiteralExpression(initializer)) {
        collect(initializer)
      } else if (
        initializer &&
        ts.isCallExpression(initializer) &&
        initializer.arguments[0] &&
        ts.isObjectLiteralExpression(initializer.arguments[0])
      ) {
        collect(initializer.arguments[0])
      }
    }
  }

  return result
}

test('native desktop window title is owned by the Ansatz product identity', () => {
  assert.equal(DESKTOP_WINDOW_TITLE, 'Ansatz')
  assert.notEqual(DESKTOP_WINDOW_TITLE, 'Hermes')
})

test('native desktop alerts and updater handoff copy use the Ansatz app identity', () => {
  const main = source('electron/main.ts')

  for (const staleAppBranding of [
    "payload?.title || 'Hermes'",
    "title: 'Hermes update'",
    "'Hermes update did not finish'",
    'Hermes will start automatically',
    'Don’t reopen Hermes',
    'Hermes will keep running'
  ]) {
    assert.equal(main.includes(staleAppBranding), false, staleAppBranding)
  }

  assert.match(main, /payload\?\.title \|\| ANSATZ_PRODUCT\.productName/)
  assert.match(main, /title: `\$\{ANSATZ_PRODUCT\.productName\} update`/)
  assert.match(main, /`\$\{ANSATZ_PRODUCT\.productName\} update did not finish`/)
  assert.equal(main.match(/Updating \$\{ANSATZ_PRODUCT\.productName\}/g)?.length, 2)
  assert.equal(main.match(/\$\{ANSATZ_PRODUCT\.productName\} will keep running/g)?.length, 2)
  assert.match(main, /let command = 'hermes update'/)
})

test('renderer-only app actions do not present Hermes as the desktop product', () => {
  const rendererSources = [
    source('src/app/settings/uninstall-section.tsx'),
    source('src/app/quick-entry/quick-entry-app.tsx'),
    source('src/app/pet-overlay/pet-overlay-app.tsx')
  ].join('\n')

  for (const staleAppBranding of ['Uninstall Hermes', 'Ask Hermes…', 'open Hermes to reconnect', 'Open in Hermes']) {
    assert.equal(rendererSources.includes(staleAppBranding), false, staleAppBranding)
  }
})

test('localized app-lifecycle copy does not call the desktop product Hermes', () => {
  const directAppPaths = [
    'settings.notifications.focusedHint',
    'settings.notifications.kinds.turnDone.description',
    'settings.notifications.kinds.plugin.description',
    'settings.notifications.testTitle',
    'settings.appearance.colorModeDesc',
    'settings.appearance.pet.restartHint',
    'settings.about.heading',
    'settings.about.automaticUpdatesDesc',
    'settings.quickEntry.enabledDesc',
    'updates.applyingBody',
    'updates.applyingBodyBackend',
    'updates.applyingClose'
  ]

  for (const locale of ['en', 'zh', 'zh-hant', 'ja', 'ar']) {
    const properties = objectPropertyInitializers(`src/i18n/${locale}.ts`)

    for (const propertyPath of directAppPaths) {
      const initializer = properties.get(propertyPath)

      if (!initializer) {
        assert.notEqual(locale, 'en', `en:${propertyPath} is missing from the base translation catalog`)
        // Partial locales intentionally inherit omitted keys from English.

        continue
      }

      assert.doesNotMatch(initializer, /\bHermes\b/, `${locale}:${propertyPath}`)
    }

    for (const propertyPath of ['restartToUseSaveImage', 'restartToSaveImages']) {
      const matches = [...properties].filter(([candidate]) => candidate.endsWith(`.${propertyPath}`))

      assert.ok(matches.length <= 1, `${locale}:${propertyPath} should identify at most one direct app action`)

      if (matches.length === 1) {
        assert.doesNotMatch(matches[0][1], /\bHermes\b/, `${locale}:${matches[0][0]}`)
      }
    }
  }
})

test('localized technical copy keeps legitimate Hermes engine and gateway terminology', () => {
  const en = objectPropertyInitializers('src/i18n/en.ts')

  assert.match(en.get('settings.connections.kindRemoteDesc') || '', /Hermes gateway/)
  assert.match(en.get('updates.availableBodyBackend') || '', /Hermes backend/)
  assert.match(en.get('updates.stages.update') || '', /Hermes/)
  assert.match(en.get('updates.stages.restart') || '', /Hermes/)
  assert.match(en.get('settings.notifications.kinds.input.description') || '', /Hermes asked/)
  assert.match(en.get('settings.appearance.pet.intro') || '', /Hermes is doing/)
})
