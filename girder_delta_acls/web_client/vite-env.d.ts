/// <reference types="vite/client" />

// Girder core is never bundled: plugin code reaches it through the `girder` global that
// core assigns to `window` before it injects any plugin script.
//
// girder-jsonforms declares `import { type Girder } from '@girder/core'` here and depends on
// `"@girder/core": "*"`. That is avoided on purpose -- the newest `@girder/core` on npm is
// 3.2.16, i.e. Girder 3, so those types describe a different major version of the API than
// this plugin targets. Depending on the local `girder/web` checkout instead would only work
// when this repo sits next to it, which breaks CI and standalone installs.
//
// Nothing typechecks this package (vite builds plain .js), so a loose declaration costs
// nothing. To get real hints while editing, point tsconfig `paths` at a local
// `girder/web/dist-lib` checkout.
declare global {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const girder: any;
}

export {};
