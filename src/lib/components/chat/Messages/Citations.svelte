<script lang="ts">
	import { getContext } from 'svelte';
	import { embed, showControls, showEmbeds } from '$lib/stores';

	import CitationModal from './Citations/CitationModal.svelte';

	const i18n = getContext('i18n');

	export let id = '';
	export let chatId = '';

	export let sources = [];
	export let readOnly = false;

	let citations = [];
	let showPercentage = false;
	let showRelevance = true;

	let citationModal = null;

	let showCitations = true;
	let showCitationModal = false;

	let selectedCitation: any = null;

	export const showSourceModal = (sourceId) => {
		let index;
		let suffix = null;

		if (typeof sourceId === 'string') {
			const output = sourceId.split('#');
			index = parseInt(output[0]) - 1;

			if (output.length > 1) {
				suffix = output[1];
			}
		} else {
			index = sourceId - 1;
		}

		if (citations[index]) {
			console.log('Showing citation modal for:', citations[index]);

			if (citations[index]?.source?.embed_url) {
				const embedUrl = citations[index].source.embed_url;
				if (embedUrl) {
					if (readOnly) {
						// Open in new tab if readOnly
						window.open(embedUrl, '_blank');
						return;
					} else {
						showControls.set(true);
						showEmbeds.set(true);
						embed.set({
							url: embedUrl,
							title: citations[index]?.source?.name || 'Embedded Content',
							source: citations[index],
							chatId: chatId,
							messageId: id,
							sourceId: sourceId
						});
					}
				} else {
					selectedCitation = citations[index];
					showCitationModal = true;
				}
			} else {
				selectedCitation = citations[index];
				showCitationModal = true;
			}
		}
	};

	function calculateShowRelevance(sources: any[]) {
		const distances = sources.flatMap((citation) => citation.distances ?? []);
		const inRange = distances.filter((d) => d !== undefined && d >= -1 && d <= 1).length;
		const outOfRange = distances.filter((d) => d !== undefined && (d < -1 || d > 1)).length;

		if (distances.length === 0) {
			return false;
		}

		if (
			(inRange === distances.length - 1 && outOfRange === 1) ||
			(outOfRange === distances.length - 1 && inRange === 1)
		) {
			return false;
		}

		return true;
	}

	function shouldShowPercentage(sources: any[]) {
		const distances = sources.flatMap((citation) => citation.distances ?? []);
		return distances.every((d) => d !== undefined && d >= -1 && d <= 1);
	}

	$: {
		citations = sources.reduce((acc, source) => {
			if (Object.keys(source).length === 0) {
				return acc;
			}

			source?.document?.forEach((document, index) => {
				const metadata = source?.metadata?.[index];
				const distance = source?.distances?.[index];

				// Within the same citation there could be multiple documents
				const id = metadata?.source ?? source?.source?.id ?? 'N/A';
				let _source = source?.source;

				if (metadata?.name) {
					_source = { ..._source, name: metadata.name };
				}

				if (id.startsWith('http://') || id.startsWith('https://')) {
					_source = { ..._source, name: id, url: id };
				}

				const existingSource = acc.find((item) => item.id === id);

				if (existingSource) {
					existingSource.document.push(document);
					existingSource.metadata.push(metadata);
					if (distance !== undefined) existingSource.distances.push(distance);
				} else {
					acc.push({
						id: id,
						source: _source,
						document: [document],
						metadata: metadata ? [metadata] : [],
						distances: distance !== undefined ? [distance] : []
					});
				}
			});

			return acc;
		}, []);
		console.log('citations', citations);

		showRelevance = calculateShowRelevance(citations);
		showPercentage = shouldShowPercentage(citations);
	}

	const decodeString = (str: string) => {
		try {
			return decodeURIComponent(str);
		} catch (e) {
			return str;
		}
	};
</script>

<CitationModal
	bind:show={showCitationModal}
	citation={selectedCitation}
	{showPercentage}
	{showRelevance}
/>

{#if citations.length > 0}
	{@const urlCitations = citations.filter((c) => c?.source?.name?.startsWith('http'))}
	<div class=" py-1 -mx-0.5 w-full flex gap-1 items-center flex-wrap">
		<button
			class="text-xs font-medium text-gray-600 dark:text-gray-300 px-3.5 h-8 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition flex items-center gap-1 border border-gray-50 dark:border-gray-850/30"
			on:click={() => {
				showCitations = !showCitations;
			}}
		>
			{#if urlCitations.length > 0}
				<div class="flex -space-x-1 items-center">
					{#each urlCitations.slice(0, 3) as citation, idx}
						<img
							src="https://www.google.com/s2/favicons?sz=32&domain={citation.source.name}"
							alt="favicon"
							class="size-4 rounded-full shrink-0 border border-white dark:border-gray-850 bg-white dark:bg-gray-900"
						/>
					{/each}
				</div>
			{/if}
			<div>
				{#if citations.length === 1}
					{$i18n.t('1 Source')}
				{:else}
					{$i18n.t('{{COUNT}} Sources', {
						COUNT: citations.length
					})}
				{/if}
			</div>
		</button>

		{#if showCitations}
			<div class="space-y-1.5 border-l-4 border-[#FFD300] pl-2">
				{#each citations as citation, idx}
					{@const fullPath = citation.source.name || 'N/A'}
					{@const rawName = fullPath?.split('/').pop()?.split('\\').pop() || fullPath}
					{@const nameParts = rawName.split('.')}
					{@const ext = nameParts.length > 1 ? '.' + nameParts.pop() : ''}
					{@const base = nameParts.join('.')}
					{@const shortBase = base.split(' - ').slice(-1)[0]}
					{@const fileName = shortBase + ext}
					{@const pageInfo = citation.metadata?.[0]?.page
						? ` - p.${citation.metadata[0].page}`
						: ''}
					{@const snippet = citation.document?.[0] || ''}
					{@const projectName = fullPath?.split('/')[0]?.split('-').slice(0, 2).join('-') || ''}
					{@const tooltipText = `${fileName}${pageInfo ? ' • Page ' + citation.metadata[0].page : ''}\n${projectName}`}
					<button
						id={`source-${id}-${idx + 1}`}
						class="w-full text-left group flex items-start gap-2 p-1.5 rounded hover:bg-gray-100/50 dark:hover:bg-gray-800/30 transition"
						title={tooltipText}
						on:click={() => {
							if (citation?.source?.embed_url) {
								window.open(citation.source.embed_url, '_blank');
							} else {
								showCitationModal = true;
								selectedCitation = citation;
							}
						}}
					>
						<span
							class="flex-shrink-0 text-[9px] font-mono font-semibold text-gray-500 dark:text-gray-400 bg-gray-200/50 dark:bg-gray-700/50 px-1.5 py-0.5 rounded min-w-[18px] text-center"
						>
							{idx + 1}
						</span>
						<div class="flex-1 min-w-0">
							<div
								class="text-[11px] font-medium text-gray-700 dark:text-gray-200 truncate group-hover:text-yellow-600 dark:group-hover:text-yellow-400 transition"
							>
								{decodeString(fileName)}
							</div>

							{#if citation.metadata?.[0]}
								<div class="flex flex-wrap gap-1 mt-0.5">
									{#if citation.metadata[0].page}
										<span
											class="text-[9px] px-1 rounded bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
										>
											p. {citation.metadata[0].page}
										</span>
									{/if}
									{#if citation.metadata[0].doc_hint}
										<span
											class="text-[9px] px-1 rounded bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200 uppercase"
										>
											{citation.metadata[0].doc_hint}
										</span>
									{/if}
									{#if citation.metadata[0].project_names}
										<span
											class="text-[9px] px-1 rounded bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 truncate max-w-[150px]"
										>
											{citation.metadata[0].project_names}
										</span>
									{/if}
									{#if citation.metadata[0].company_names}
										<span
											class="text-[9px] px-1 rounded bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200 truncate max-w-[150px]"
										>
											{citation.metadata[0].company_names}
										</span>
									{/if}
								</div>
							{/if}

							{#if citation.document?.[0]}
								<div class="text-[10px] text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">
									"{citation.document[0].substring(0, 150)}..."
								</div>
							{/if}
						</div>
					</button>
				{/each}
			</div>
		{:else}
			<!-- Collapsed preview -->
			<div class="flex gap-1.5 flex-wrap">
				{#each citations.slice(0, 3) as citation, idx}
					{@const fileName =
						citation.source.name?.split('/').pop()?.split('\\').pop() || citation.source.name}
					<span
						class="text-[11px] text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/20 px-2 py-0.5 rounded-full"
					>
						{decodeString(fileName).substring(0, 30)}{decodeString(fileName).length > 30
							? '...'
							: ''}
					</span>
				{/each}
				{#if citations.length > 3}
					<span class="text-[11px] text-gray-500 dark:text-gray-500 px-1">
						+{citations.length - 3}
					</span>
				{/if}
			</div>
		{/if}
	</div>
{/if}
