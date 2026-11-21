# Research report: transformer architectures

*Topic:* transformer architectures  
*Generated:* 2026-11-21T04:28:48.813053+00:00

> **Note:** Generated in offline mock mode: the language model only extracts sentences, so this report does not reflect real model quality.

## Executive summary

- The Gamma Transformer uses convolutional token mixing and the C4 corpus. [1]
- We evaluate the Beta Transformer on the WikiText dataset. [3]
- A limitation of the Beta Transformer is reduced accuracy on copy tasks. [5]
- Future work remains open for hardware efficient kernels. [6]

## Methodologies and Architectures

- The Gamma Transformer uses convolutional token mixing and the C4 corpus. [1]
- The Alpha Transformer uses sparse attention over local windows. [2]
- The Beta Transformer builds on the Alpha Transformer with linear attention. [3]

## Datasets and Benchmarks

- We evaluate the Beta Transformer on the WikiText dataset. [3]
- Gamma Transformer reaches accuracy of 71.2 percent on the GLUE benchmark. [4]
- We train on the WikiText dataset using the Adam optimizer. [2]

## Reported Quantitative Results

| Document | Reported result (verbatim) | Numbers | Page |
|---|---|---|---|
| alpha | Alpha Transformer reaches perplexity of 18.3 on WikiText. | 18.3 | 2 [7] |
| beta | Beta Transformer reaches perplexity of 21.9 on WikiText. | 21.9 | 2 [8] |
| gamma | Gamma Transformer reaches accuracy of 71.2 percent on the GLUE benchmark. | 71.2 | 2 [4] |

_Results are quoted as written. Values from different datasets, metrics or settings are not normalised and may not be comparable._

## Limitations

- A limitation of the Beta Transformer is reduced accuracy on copy tasks. [5]
- Alpha Transformer suffers from quadratic memory on very long sequences. [6]

## Possible Contradictions and Inconsistencies

- [numeric candidate] "Alpha Transformer reaches perplexity of 18.3 on WikiText." (alpha) vs "Beta Transformer reaches perplexity of 21.9 on WikiText." (beta). Shared terms: perplexity, reach, transformer, wikitext. [7][8]

_Heuristic candidates only: the sources may describe different models, datasets or settings. Verify manually._

## Research Gaps and Opportunities Stated in the Sources

- Future work remains open for hardware efficient kernels. [6]
- Whether linear attention scales to billions of parameters is an open problem. [5]

_These gaps are stated by the sources themselves; NEXUS does not infer additional ones._

## Documents analysed

- beta (beta.pdf), versions [1], pages seen [1, 2]
- alpha (alpha.pdf), versions [1], pages seen [1, 2]
- gamma (gamma.pdf), versions [1], pages seen [1, 2]

## Coverage notes

- 3 document(s) analysed from 3 with retrieved evidence.

## Sources

[1] gamma (gamma.pdf) - p.1, section: 1. METHODS, v1  
> The Gamma Transformer uses convolutional token mixing and the C4 corpus.
[2] alpha (alpha.pdf) - p.1, section: 1. METHODS, v1  
> The Alpha Transformer uses sparse attention over local windows.
> We train on the WikiText dataset using the Adam optimizer.
[3] beta (beta.pdf) - p.1, section: 1. METHODS, v1  
> The Beta Transformer builds on the Alpha Transformer with linear attention.
> We evaluate the Beta Transformer on the WikiText dataset.
[4] gamma (gamma.pdf) - p.2, section: 2. RESULTS, v1  
> Gamma Transformer reaches accuracy of 71.2 percent on the GLUE benchmark.
[5] beta (beta.pdf) - p.2, section: 3. LIMITATIONS, v1  
> A limitation of the Beta Transformer is reduced accuracy on copy tasks.
> Whether linear attention scales to billions of parameters is an open problem.
[6] alpha (alpha.pdf) - p.2, section: 3. LIMITATIONS, v1  
> Alpha Transformer suffers from quadratic memory on very long sequences.
> Future work remains open for hardware efficient kernels.
[7] alpha (alpha.pdf) - p.2, section: 2. RESULTS, v1  
> Alpha Transformer reaches perplexity of 18.3 on WikiText.
[8] beta (beta.pdf) - p.2, section: 2. RESULTS, v1  
> Beta Transformer reaches perplexity of 21.9 on WikiText.

## Method and limitations

Every cited statement is either a claim verified against the passages it cites (content coverage, exact numbers) or a verbatim source sentence. Contradiction and gap sections are heuristic and flagged. Nothing is added from model memory.

Claims verified and kept: 8 of 8 considered; unsupported claims removed: 0; dropped by final numeric check: 0.

_Statements are either claims verified against the cited passages or verbatim source sentences. Sections marked heuristic list candidates only. Verify important findings in the original documents._
