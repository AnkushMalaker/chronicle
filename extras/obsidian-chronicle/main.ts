import {
  App,
  ButtonComponent,
  Modal,
  Notice,
  Plugin,
  PluginSettingTab,
  SearchComponent,
  SecretComponent,
  Setting,
  TFile,
  requestUrl,
} from "obsidian";

interface ChronicleSettings {
  baseUrl: string;
  secretName: string;
  dismissedSuggestions: string[];
  bootstrapApiKey?: string;
}

interface SuggestedPerson {
  name: string;
  path: string;
  hash: string;
  snippets: string[];
}

interface PersonSuggestion {
  pair_id: string;
  revision: string;
  score: number;
  reasons: string[];
  person_a: SuggestedPerson;
  person_b: SuggestedPerson;
}

interface MetadataConflict {
  field: string;
  source_value: unknown;
  target_value: unknown;
}

interface MergePreview {
  source_name: string;
  target_name: string;
  source_path: string;
  target_path: string;
  plan_token: string;
  facts_to_add: number;
  duplicate_facts_skipped: number;
  backlink_files: string[];
  backlink_occurrences: number;
  metadata_conflicts: MetadataConflict[];
}

const DEFAULT_SETTINGS: ChronicleSettings = {
  baseUrl: "",
  secretName: "",
  dismissedSuggestions: [],
};

async function sha256(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function personName(file: TFile): string | null {
  return file.parent?.path === "People" ? file.basename : null;
}

export default class ChronicleCompanionPlugin extends Plugin {
  settings: ChronicleSettings = DEFAULT_SETTINGS;

  async onload(): Promise<void> {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    await this.migrateBootstrapApiKey();
    this.addSettingTab(new ChronicleSettingTab(this.app, this));
    this.addCommand({
      id: "merge-current-person",
      name: "Merge current person…",
      checkCallback: (checking) => {
        const source = this.app.workspace.getActiveFile();
        if (!source || !personName(source)) return false;
        if (!checking) new PersonPickerModal(this, source).open();
        return true;
      },
    });
    this.addCommand({
      id: "review-duplicate-people",
      name: "Review possible duplicate people…",
      callback: () => void this.reviewDuplicatePeople(),
    });
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }

  private async migrateBootstrapApiKey(): Promise<void> {
    if (!this.settings.bootstrapApiKey) return;
    const secretName = "chronicle-companion-api-key";
    this.app.secretStorage.setSecret(secretName, this.settings.bootstrapApiKey);
    this.settings.secretName = secretName;
    delete this.settings.bootstrapApiKey;
    await this.saveSettings();
  }

  private apiKey(): string {
    if (!this.settings.secretName) {
      throw new Error("Choose a Chronicle API key in the plugin settings.");
    }
    const value = this.app.secretStorage.getSecret(this.settings.secretName);
    if (!value) throw new Error("The selected Chronicle API key is empty.");
    return value;
  }

  private endpoint(path: string): string {
    const base = this.settings.baseUrl.trim().replace(/\/+$/, "");
    if (!base) throw new Error("Set the Chronicle server URL in the plugin settings.");
    return `${base}${path}`;
  }

  async previewMerge(source: TFile, target: TFile): Promise<MergePreview> {
    const [sourceText, targetText] = await Promise.all([
      this.app.vault.read(source),
      this.app.vault.read(target),
    ]);
    const response = await requestUrl({
      url: this.endpoint("/api/memories/people/merge/preview"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        source_name: source.basename,
        target_name: target.basename,
        source_hash: await sha256(sourceText),
        target_hash: await sha256(targetText),
      }),
    });
    return response.json as MergePreview;
  }

  async applyMerge(preview: MergePreview): Promise<void> {
    await requestUrl({
      url: this.endpoint("/api/memories/people/merge"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        source_name: preview.source_name,
        target_name: preview.target_name,
        plan_token: preview.plan_token,
      }),
    });
  }

  async getSuggestions(): Promise<PersonSuggestion[]> {
    const response = await requestUrl({
      url: this.endpoint("/api/memories/people/suggestions?limit=30"),
      method: "GET",
      headers: { Authorization: `Bearer ${this.apiKey()}` },
    });
    return (response.json as { suggestions: PersonSuggestion[] }).suggestions;
  }

  async markSeparate(suggestion: PersonSuggestion): Promise<void> {
    await requestUrl({
      url: this.endpoint("/api/memories/people/identity"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        person_a: suggestion.person_a.name,
        person_b: suggestion.person_b.name,
        decision: "distinct",
        revision: suggestion.revision,
      }),
    });
  }

  async dismissSuggestion(suggestion: PersonSuggestion): Promise<void> {
    const dismissal = `${suggestion.pair_id}:${suggestion.revision}`;
    this.settings.dismissedSuggestions = [
      ...new Set([...this.settings.dismissedSuggestions, dismissal]),
    ].slice(-500);
    await this.saveSettings();
  }

  private async reviewDuplicatePeople(): Promise<void> {
    new Notice("Looking for possible duplicate people…");
    try {
      const suggestions = await this.getSuggestions();
      const dismissed = new Set(this.settings.dismissedSuggestions);
      const pending = suggestions.filter(
        (suggestion) => !dismissed.has(`${suggestion.pair_id}:${suggestion.revision}`),
      );
      if (!pending.length) {
        new Notice("No new possible duplicate people found.");
        return;
      }
      new DuplicateReviewModal(this, pending).open();
    } catch (error) {
      new Notice(`Could not load suggestions: ${errorMessage(error)}`, 8000);
    }
  }
}

class DuplicateReviewModal extends Modal {
  private readonly plugin: ChronicleCompanionPlugin;
  private readonly suggestions: PersonSuggestion[];
  private index = 0;
  private acting = false;

  constructor(plugin: ChronicleCompanionPlugin, suggestions: PersonSuggestion[]) {
    super(plugin.app);
    this.plugin = plugin;
    this.suggestions = suggestions;
  }

  onOpen(): void {
    this.render();
  }

  private render(): void {
    this.contentEl.empty();
    const suggestion = this.suggestions[this.index];
    if (!suggestion) {
      this.close();
      new Notice("Duplicate review complete.");
      return;
    }

    this.setTitle("Possible duplicate people");
    const header = this.contentEl.createDiv({ cls: "chronicle-identity-header" });
    header.createEl("p", {
      cls: "chronicle-merge-help",
      text: `Suggestion ${this.index + 1} of ${this.suggestions.length} · confidence ${suggestion.score}`,
    });
    const navigation = header.createDiv({ cls: "chronicle-identity-navigation" });
    new ButtonComponent(navigation)
      .setButtonText("Back")
      .setDisabled(this.index === 0)
      .onClick(() => this.move(-1));
    new ButtonComponent(navigation)
      .setButtonText("Next")
      .setDisabled(this.index === this.suggestions.length - 1)
      .onClick(() => this.move(1));
    const comparison = this.contentEl.createDiv({
      cls: "chronicle-identity-comparison",
    });
    this.addPerson(comparison, suggestion.person_a);
    this.addPerson(comparison, suggestion.person_b);

    const evidence = this.contentEl.createDiv({ cls: "chronicle-identity-evidence" });
    evidence.createDiv({
      cls: "chronicle-identity-evidence-title",
      text: "Why Chronicle suggested this",
    });
    const reasons = evidence.createEl("ul");
    for (const reason of suggestion.reasons) reasons.createEl("li", { text: reason });

    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: "Separate people is a durable vault annotation. Not sure hides only this version of the suggestion.",
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-identity-actions" });
    new ButtonComponent(actions)
      .setButtonText("Not sure")
      .onClick(() => void this.dismiss());
    new ButtonComponent(actions)
      .setButtonText("Separate people")
      .onClick(() => void this.separate());
    new ButtonComponent(actions)
      .setButtonText("Same person…")
      .setCta()
      .onClick(() => {
        this.close();
        new CanonicalPersonModal(this.plugin, suggestion).open();
      });
  }

  private move(offset: number): void {
    const nextIndex = this.index + offset;
    if (nextIndex < 0 || nextIndex >= this.suggestions.length) return;
    this.index = nextIndex;
    this.render();
  }

  private removeCurrent(): void {
    this.suggestions.splice(this.index, 1);
    if (this.index >= this.suggestions.length) this.index = this.suggestions.length - 1;
    this.render();
  }

  private addPerson(parent: HTMLElement, person: SuggestedPerson): void {
    const card = parent.createDiv({ cls: "chronicle-identity-person" });
    card.createEl("h3", { text: person.name });
    card.createEl("div", { cls: "chronicle-merge-path", text: person.path });
    if (person.snippets.length) {
      const snippets = card.createEl("ul");
      for (const snippet of person.snippets.slice(0, 3)) {
        snippets.createEl("li", { text: snippet });
      }
    }
    const open = card.createEl("button", { text: "Open note" });
    open.addEventListener("click", () => void this.openNote(person.path));
  }

  private async openNote(path: string): Promise<void> {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!(file instanceof TFile)) {
      new Notice(`Could not find ${path}. The vault may still be syncing.`);
      return;
    }
    await this.app.workspace.getLeaf("tab").openFile(file);
  }

  private async dismiss(): Promise<void> {
    if (this.acting) return;
    this.acting = true;
    await this.plugin.dismissSuggestion(this.suggestions[this.index]);
    this.acting = false;
    this.removeCurrent();
  }

  private async separate(): Promise<void> {
    if (this.acting) return;
    this.acting = true;
    const suggestion = this.suggestions[this.index];
    try {
      await this.plugin.markSeparate(suggestion);
      new Notice(`${suggestion.person_a.name} and ${suggestion.person_b.name} marked as separate.`);
      this.acting = false;
      this.removeCurrent();
    } catch (error) {
      this.acting = false;
      new Notice(`Could not save decision: ${errorMessage(error)}`, 8000);
    }
  }
}

class CanonicalPersonModal extends Modal {
  private readonly plugin: ChronicleCompanionPlugin;
  private readonly suggestion: PersonSuggestion;

  constructor(plugin: ChronicleCompanionPlugin, suggestion: PersonSuggestion) {
    super(plugin.app);
    this.plugin = plugin;
    this.suggestion = suggestion;
  }

  onOpen(): void {
    const { person_a: personA, person_b: personB } = this.suggestion;
    this.setTitle("Which name should Chronicle keep?");
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: "The other name is retained as an alias, and links are rewritten after a final preview.",
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-canonical-actions" });
    new ButtonComponent(actions)
      .setButtonText(`Keep ${personA.name}`)
      .setCta()
      .onClick(() => void this.choose(personB, personA));
    new ButtonComponent(actions)
      .setButtonText(`Keep ${personB.name}`)
      .setCta()
      .onClick(() => void this.choose(personA, personB));
  }

  private async choose(sourcePerson: SuggestedPerson, targetPerson: SuggestedPerson): Promise<void> {
    const source = this.app.vault.getAbstractFileByPath(sourcePerson.path);
    const target = this.app.vault.getAbstractFileByPath(targetPerson.path);
    if (!(source instanceof TFile) || !(target instanceof TFile)) {
      new Notice("One of the person notes is not available yet. Wait for vault sync and retry.");
      return;
    }
    this.close();
    new Notice("Checking that the Chronicle vault is in sync…");
    try {
      const preview = await this.plugin.previewMerge(source, target);
      new MergePreviewModal(this.plugin, preview).open();
    } catch (error) {
      new Notice(`Could not preview merge: ${errorMessage(error)}`, 8000);
    }
  }
}

class PersonPickerModal extends Modal {
  private readonly plugin: ChronicleCompanionPlugin;
  private readonly source: TFile;
  private people: TFile[] = [];
  private listEl!: HTMLDivElement;

  constructor(plugin: ChronicleCompanionPlugin, source: TFile) {
    super(plugin.app);
    this.plugin = plugin;
    this.source = source;
  }

  onOpen(): void {
    this.setTitle(`Merge ${this.source.basename} into…`);
    this.people = this.app.vault
      .getMarkdownFiles()
      .filter((file) => personName(file) && file.path !== this.source.path)
      .sort((left, right) => left.basename.localeCompare(right.basename));

    const search = new SearchComponent(this.contentEl);
    search.setPlaceholder("Find the canonical person");
    search.inputEl.setAttr("aria-label", "Find the canonical person");
    search.onChange((query) => this.renderPeople(query));
    this.listEl = this.contentEl.createDiv({ cls: "chronicle-merge-list" });
    this.renderPeople("");
    search.inputEl.focus();
  }

  private renderPeople(query: string): void {
    this.listEl.empty();
    const normalized = query.trim().toLocaleLowerCase();
    const matches = this.people.filter((file) =>
      file.basename.toLocaleLowerCase().includes(normalized),
    );
    for (const file of matches) {
      const button = this.listEl.createEl("button", {
        cls: "chronicle-merge-person clickable-icon",
        text: file.basename,
      });
      button.setAttr("aria-label", `Merge ${this.source.basename} into ${file.basename}`);
      button.addEventListener("click", () => void this.choose(file));
    }
    if (!matches.length) {
      this.listEl.createDiv({ cls: "chronicle-merge-help", text: "No matching people." });
    }
  }

  private async choose(target: TFile): Promise<void> {
    this.close();
    new Notice("Checking that the Chronicle vault is in sync…");
    try {
      const preview = await this.plugin.previewMerge(this.source, target);
      new MergePreviewModal(this.plugin, preview).open();
    } catch (error) {
      new Notice(`Could not preview merge: ${errorMessage(error)}`, 8000);
    }
  }
}

class MergePreviewModal extends Modal {
  private readonly plugin: ChronicleCompanionPlugin;
  private readonly preview: MergePreview;
  private applying = false;

  constructor(plugin: ChronicleCompanionPlugin, preview: MergePreview) {
    super(plugin.app);
    this.plugin = plugin;
    this.preview = preview;
  }

  onOpen(): void {
    this.setTitle(`Merge ${this.preview.source_name} into ${this.preview.target_name}?`);
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-path",
      text: `${this.preview.source_path} → ${this.preview.target_path}`,
    });
    const summary = this.contentEl.createDiv({ cls: "chronicle-merge-summary" });
    this.addStat(summary, String(this.preview.facts_to_add), "facts added");
    this.addStat(summary, String(this.preview.backlink_occurrences), "links rewritten");
    this.addStat(summary, String(this.preview.backlink_files.length), "notes updated");
    this.addStat(
      summary,
      String(this.preview.duplicate_facts_skipped),
      "duplicates skipped",
    );

    if (this.preview.metadata_conflicts.length) {
      this.contentEl.createEl("p", {
        text: "The canonical note keeps these existing values:",
      });
      const conflicts = this.contentEl.createEl("ul", { cls: "chronicle-merge-conflicts" });
      for (const conflict of this.preview.metadata_conflicts) {
        conflicts.createEl("li", {
          text: `${conflict.field}: ${String(conflict.target_value)} (discarding ${String(conflict.source_value)})`,
        });
      }
    }

    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: `“${this.preview.source_name}” will be retained as an alias. Chronicle will audit every changed note.`,
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-merge-actions" });
    new ButtonComponent(actions).setButtonText("Cancel").onClick(() => this.close());
    new ButtonComponent(actions)
      .setButtonText("Merge person")
      .setWarning()
      .onClick(() => void this.apply());
  }

  private addStat(parent: HTMLElement, value: string, label: string): void {
    const stat = parent.createDiv({ cls: "chronicle-merge-stat" });
    stat.createSpan({ cls: "chronicle-merge-stat-value", text: value });
    stat.createSpan({ cls: "chronicle-merge-stat-label", text: label });
  }

  private async apply(): Promise<void> {
    if (this.applying) return;
    this.applying = true;
    try {
      await this.plugin.applyMerge(this.preview);
      this.close();
      new Notice(
        `Merged ${this.preview.source_name} into ${this.preview.target_name}. Waiting for vault sync.`,
        8000,
      );
    } catch (error) {
      this.applying = false;
      new Notice(`Merge failed: ${errorMessage(error)}`, 8000);
    }
  }
}

class ChronicleSettingTab extends PluginSettingTab {
  private readonly plugin: ChronicleCompanionPlugin;

  constructor(app: App, plugin: ChronicleCompanionPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    this.containerEl.empty();
    new Setting(this.containerEl)
      .setName("Chronicle server")
      .setDesc("HTTPS address reachable from this device")
      .addText((text) =>
        text
          .setPlaceholder("https://chronicle.example.com")
          .setValue(this.plugin.settings.baseUrl)
          .onChange(async (value) => {
            this.plugin.settings.baseUrl = value.trim();
            await this.plugin.saveSettings();
          }),
      );
    new Setting(this.containerEl)
      .setName("Chronicle API key")
      .setDesc("Select or create a long-lived Chronicle API key in Obsidian SecretStorage")
      .addComponent((element) =>
        new SecretComponent(this.app, element)
          .setValue(this.plugin.settings.secretName)
          .onChange(async (value) => {
            this.plugin.settings.secretName = value;
            await this.plugin.saveSettings();
          }),
      );
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
