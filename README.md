## Future Translations
Please do not fork this repository if you want your translations to be added to MyPet.<br />
There is a dedicated website for MyPet translations that can be found under:

* https://translation.mypet-plugin.de

## Branches
This repository follows the MyPet branch pipeline: `staging` → `alpha` → `main`.

* Pull requests (including Crowdin's) target `staging`, the default branch. They are squash-merged.
* `alpha` feeds MyPet alpha builds and `main` feeds MyPet releases; both are updated only by promotion PRs.
* A translation change that belongs to a plugin change uses the same branch name as the plugin PR and names it in the PR's **Siblings** section.

One-time manual step (owner): in Crowdin's GitHub integration, set the branch to `staging`, so Crowdin reads sources from and opens its PRs into `staging` instead of `main`.
