// The steps of the first-use tour, as data.
//
// Each step points at one element of the app. `targets` is a list of CSS
// selectors, tried in order; the first one that is on screen is highlighted.
// A later selector is a fallback, for example the header button of a panel
// that is closed. With no target on screen the text is shown in the middle
// of the window without a pointer.
//
// Elements are marked in the app with a `data-tour` attribute.

export type TourStep = {
  id: string;
  title: string;
  text: string;
  targets: string[];
};

const mark = (name: string) => `[data-tour="${name}"]`;

export const TOUR_STEPS: TourStep[] = [
  {
    id: 'welcome',
    title: 'Welcome to inLUMEN',
    text: 'In inLUMEN you design a data pipeline as a graph of steps. This short tour shows the main parts of the workspace. You can skip it at any time and open it again from Help.',
    targets: [],
  },
  {
    id: 'library',
    title: 'Component library',
    text: 'The left panel lists the building blocks of a pipeline, such as sources, tasks and destinations. Drag one onto the canvas to add it. The Library button in the header shows or hides this panel.',
    targets: [mark('library'), mark('library-toggle')],
  },
  {
    id: 'canvas',
    title: 'Canvas',
    text: 'The pipeline is drawn here. Drag a step to move it. To connect two steps, drag from a port of one step to a port of the other.',
    targets: [mark('canvas')],
  },
  {
    id: 'chat',
    title: 'Chat',
    text: 'Describe the pipeline you want in plain words, and the assistant draws it on the canvas. The chat needs a language model, which you set up in Settings. The Chat button in the header opens this panel.',
    targets: [mark('chat'), mark('chat-toggle')],
  },
  {
    id: 'inspector',
    title: 'Inspector',
    text: 'Select a step on the canvas to see and change its settings in the Inspector. The Inspector button in the header opens this panel.',
    targets: [mark('inspector'), mark('inspector-toggle')],
  },
  {
    id: 'canvas-actions',
    title: 'Save, import and export',
    text: 'Save stores the pipeline as a named version. JSON downloads the project as a file. Import loads a Project JSON file, an inLUMEN bundle ZIP, an Argo Workflows file or a ZIP of code.',
    targets: [mark('canvas-actions')],
  },
  {
    id: 'run',
    title: 'Code and Run',
    text: 'The Run tab of the left panel generates code for the steps and runs the pipeline. The results and output files of a run are shown there too.',
    targets: [mark('run-tab'), mark('library-toggle')],
  },
  {
    id: 'help',
    title: 'Help',
    text: 'Help has short tips for working in inLUMEN. You can start this tour again from there at any time.',
    targets: [mark('help')],
  },
];
