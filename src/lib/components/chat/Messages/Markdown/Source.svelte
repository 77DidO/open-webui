<script lang="ts">
	import { decodeString } from '$lib/utils';

	export let id;

	export let title: string = 'N/A';

	export let onClick: Function = () => {};

	// Helper function to return only the domain from a URL
	function getDomain(url: string): string {
		const domain = url.replace('http://', '').replace('https://', '').split(/[/?#]/)[0];

		if (domain.startsWith('www.')) {
			return domain.slice(4);
		}
		return domain;
	}

	const getDisplayTitle = (title: string) => {
		if (!title) return 'N/A';
		if (title.length > 30) {
			return title.slice(0, 15) + '...' + title.slice(-10);
		}
		return title;
	};

	// Helper function to check if text is a URL and return the domain
	function formattedTitle(title: string): string {
		if (title.startsWith('http')) {
			return getDomain(title);
		}

		return title;
	}
</script>

<button
	class="text-[10px] w-fit translate-y-[2px] px-2 py-0.5 dark:bg-gray-800 dark:text-[#FFD300] bg-yellow-50 text-[#FFD300] transition rounded-xl font-bold"
	on:click={() => {
		onClick(id);
	}}
>
	<span class="line-clamp-1">
		{getDisplayTitle(formattedTitle(decodeString(title)))}
	</span>
</button>
