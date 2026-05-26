


Browser refactor
    - What do we need to show here
        - Topics
        - Knowledge entries
            - Title, Content, Type
        - Flashcards
            - Question, Answer, Due date



        - Resources (potentially, though might be reundant with the resources panel)

        - Review Sessions

    - Behaviours
        - Select a topic
        - Create a topic
        - Delete a topic

        - Modify knowledge entries/flashcards
        - Search entries/flashcards by title/content

        - Multi-select knowledge/entries
            - move them
            - delete them
            - request some change to them by the agent

        - Filter knowledge entries by type

        - Sort knowledge entries by
            - type
            - title

        - Sort flashcards by
            - question
            - due
            - possibly by "marked" (if this is in the DB schema - not sure it is yet)
            - knowledge entry
        
        - See which flashcards are linked to which knowledge entries

    - Input area to ask a subagent to do something in the browser


    - (Future) Agent basically needs CRUD tools on the major data types in the DB






Widget architecture:

    Topic Tree on the left
    "Browser Tab" on the right
    "Browser Tabs" above the "Browser Tab"

    Browser Tab is an _abstract view_ - a notion of "what data is displayed" which is _modulated_ by the TopicTree

    Fluff:
        ChatArea below - specialized chat area plugged into a subagent with direct DB/VM access, can be used to perform specialized actions
            - bring up a certain selection
            - perform a certain modification
            - etc.

    Because we're using a VM/View separation, this can be BOTH a feed widget AND a tab, so the user can expand^ to a tab if they want



- Remark:
    - Just make the topic tree multi-selectable by default
        - No reason to just have 1 topic selected at a time
        - default will be "no topics selected", which does _no filtering_ by topic



Browser Tabs
    Knowledge Entries

        Simplest one: displays knowledge entries plus entry title/detail box

        Displays as a table, alternating background colour by row

        [search bar]

        ID | Title | Topic | Type | Linked Flashcards   Entry Title Box
        row1                                            Entry Content Box
        row2
        ...

        
        Filtered on topics selected in the topic tree tab

        Search bar filters on topic & content?

        Can be filtered on title, type, content, flashcards
            - e.g. filter by "title is like {str}", or "type is {type}"

        Can sort on ID, title, type, content

        Can possibly toggle between the entry title/content on the right and the linked flashcards on the right


        

        Need to be able to modify everything except ID in this tab
            - Title - modifyable via the entry title box
            - Content - modifyable via the entry content box
            - Type
                - can be modified in bulk with multiselect
                - new type selection is done in a separate area (possibly below the entry content box)
                - needs to be confirmed with "enter"
            - Topic
                - can be modified in bulk with multiselect
                - new topic selection is done by "reusing" the topic tree on the left
                    - instead of showing the selected topics, it's replaced by a "single cursor" topic tree (a knowledge entry belongs to exactly one topic)
                - needs to be confirmed with "enter"
            - Linked flashcards
                - cannot be done with multiselect
                - on the right, it shows a (filterable, sortable) table of flashcards
                    - possibly with a search bar to further refine
                - can toggle which flashcards are linked to the chosen knowledge entry
                - confirmed with "enter"


        Multiselect by default hides the entry title box/entry content box
        Multiselect does NOT hide the linked flashcards, but disables relinking (ambiguous)




Architecture of what we're implementing:
    BrowserViewModel + BrowserView

    BrowserTopicTreeViewModel + BrowserTopicTreeView

    BrowserTabViewModel (abstract)

    
    
    KnowledgeEntryBrowserTabViewModel -> BrowserTabViewModel
    + KnowledgeEntryBrowserTabView

    Composed of:
        - KnowledgeEntryTableVM + View
        - FlashcardTableVM + View
        - EntryDetailsVM + View

    Each of KnowledgeEntryTableVM + View also requires a SearchBar (possibly not a VM for this though)



    BrowserTopicTreeVM needs bidirectional communication with the 
        





    Flashcards
        Very similar in principle

        ALSO shows

