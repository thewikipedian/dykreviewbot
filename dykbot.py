# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import pywikibot as pwb
import mwparserfromhell as mwp 
import urllib
import re

import xml.etree.ElementTree as ET
from datetime import timedelta, datetime
from dateutil import parser

from pudb import set_trace

import sys
reload(sys)  # Reload does the trick!
sys.setdefaultencoding('UTF8')

site = pwb.Site()

def checkbotsallowed(text):
	exclusion_tp = mwp.parse(text).filter_templates()
	for template in exclusion_tp:
		if template.name == 'nobots':
			raise NoBotsException
		if template.name == 'bots' and ((template.has('allow') and template.get('allow').value == 'none') or
			(template.has('deny') and 'all' in template.get("deny").value) or 'DYKReviewBot' in template.get("deny").value):
			raise NoBotsException

class MalformedException(Exception):
	def __init__(self, problem):
		self.problem = problem

	def __str__(self):
		return self.problem

class NoBotsException(Exception):
	def __init__(self):
		pass 

class Nomination(object):
	def __init__(self, article, nomtimestamp):
		# Parse the nom page
		self.article = Article(title=article)
		self.article_title = article 
		self.nomtimestamp = nomtimestamp
		self.length = self.article.getReadableLength()
		self.Status = DYKStatus(article_title=self.article_title)

	def checkLongEnough(self):
		self.Status.length = self.length
		self.Status.LongEnough = self.length > 1500

	def checkNewEnough(self):
		# Check if the article was created in the past 7 days
		# If not, check if article now is more than 5x what it was 7 days before nom
		# Assume the page is new unless shown otherwise	
		# Eligibility criteria
		self.isNew = True
		#set_trace()
		EXP5x = False
		BLP2x = False
		RECENT_GA = False 

		if not self.Status.GA:
			history = self.article.page.revisions(content=True)
			for revision in history:
				raw_length = len(revision['text'])
				if raw_length < 15000:
					old_version = Article(text=revision['text'])
					old_length = old_version.getReadableLength()
					if old_length < 50:
						break
				else:
					print "Skip for performance, too long"
				editor = revision['user']
				timestamp = revision['timestamp']
				comment = revision['comment']
				if self.nomtimestamp - timestamp > timedelta(8):
					old_version = Article(text=revision['text'])
					old_length = old_version.getReadableLength()
					self.isNew = False
					break
				if 'moved page [[User:' in comment or 'moved page [[Draft:' in comment:
					old_version = Article(text=revision['text'])
					old_length = old_version.getReadableLength()
					# If page used to be in userspace or draftspace, qualifies as new
					break

			self.Status.timestamp = timestamp
			self.Status.old_length = old_length
			self.Status.isNew = self.isNew

			EXP5x = float(self.length) / old_length >= 5
			BLP2x = self.Status.BLP is True and (float(self.length) / old_length >= 2)

		elif self.Status.GA is True:
			talk = pwb.Page(site, "Talk:" + self.article_title)
			talk_templates = mwp.parse(talk.text).filter_templates(matches="(Article history)|GA")
			for template in talk_templates:
				if template.name == "Article history":
					for i in xrange(10):
						if template.has("action"+str(i)):
							if template.get("action"+str(i)).value == "GAN" and template.get("action"+str(i)+"result").value == "Listed":
								self.Status.timestamp = parser.parse(str(template.get("action"+str(i)+"date").value).replace("(UTC)",""))
								self.Status.GAN = template.get("action"+str(i)+"link").value
						else:
							break
				elif template.name == "GA":
					if template.has("date"):
						date = template.get("date").value 
					else:
						date = template.get("1").value 

					self.Status.timestamp = parser.parse(str(date).replace("(UTC)", ""))
					self.Status.GAN = "/GA"+str(template.get("page").value)

			RECENT_GA = self.Status.GA is True and (self.nomtimestamp - self.Status.timestamp < timedelta(8))

		if self.isNew or EXP5x or BLP2x or RECENT_GA:
			self.Status.NewEnough = True

	def checkBLPGA(self):
		for cat in self.article.page.categories():
			if str(cat) == "[[en:Category:Living people]]":
				self.Status.BLP = True
			elif str(cat) == "[[en:Category:Good articles]]":
				self.Status.GA = True

	def checkCited(self):
		for i, paragraph in enumerate(self.article.paragraphs[self.article.lede_length:]):
			if "__REF__" not in paragraph and len(paragraph) > 100:
				words = paragraph.split()
				self.Status.UncitedParagraphs.append('[' + str(i+2) + '] (' + words[0] + " ... " + words[-1] + ')')

	def checkCopyVio(self):
		url = "https://tools.wmflabs.org/copyvios/?lang=en&project=wikipedia&{article_title}&oldid=&action=search&use_engine=1&use_links=1&turnitin=1".format(
			article_title=urllib.urlencode({'title':self.article.title.encode('utf-8')},'utf-8'))
		self.Status.earwiglink = url
		page = urllib.urlopen(url).read()
		confidence = map(float, re.search(r'<div>(\d+\.\d+)%<\/div>', page).groups())
		confidence = max(confidence)
		self.Status.NoCopyvio = confidence < 20
		self.Status.CopyvioPct = confidence

	def checkMaintenanceTags(self):
		# This has false positives and false negatives, but faster than searching all categories
		tags = []
		for template in self.article.wikitext.filter_templates():
			if template.has("date") and "cite" not in template.name and "Cite" not in template.name and "use" not in template.name:
				tags.append((template.name.strip(), template.get("date").value))
			elif "stub" in template.name:
				tags.append((template.name, "creation"))
		self.Status.MaintenanceTags = tags

	def checkNomination(self, post=True):
		self.checkLongEnough()
		self.checkBLPGA()
		self.checkNewEnough()
		self.checkCopyVio()
		self.checkCited()
		self.checkMaintenanceTags()
		self.Status.review()
		return self.Status

class Text(object):
	def __init__(self, text=None):
		self.text = mwp.parse(text)
		self.readable_text = None

	def preprocess(self):
		self.replaceRefs() # Keep track of where refs are for citation check
		self.deleteTables()
		# Delete two levels
		self.deleteTemplates()
		self.deleteNoncontentHTML()
		self.deleteLinks()
		self.deleteExtLinks()
		self.deleteMiscTags()
		self.readable_text = self.text
		self.deleteRefs()

	def getReadableLength(self):
		if self.readable_text is not None:
			return len(self.readable_text)
		else:
			self.preprocess()
			return len(self.readable_text)

	def deleteTables(self):
		for table in self.text.filter_tags(matches='^\{\|'):
			self.text.remove(table)

	def deleteLinks(self):
		for link in set(self.text.filter_wikilinks()):
			try:
				if "File:" in link.title or "Image:" in link.title:
					self.text.remove(link)
				elif "Category:" in link.title:
					self.text.remove(link)
				elif link.text is not None:
					self.text.replace(link, link.text)
				else:
					self.text.replace(link, link.title)
			except ValueError:
				continue

	def deleteBoldIt(self):
		for tag in self.text.filter_tags("^''"):
			self.text.replace(str(tag), str(tag.contents))

	def deleteExtLinks(self):
		for extlink in self.text.filter_external_links():
			self.text.replace(str(extlink), str(extlink.title))

	def deleteHeadings(self):
		for heading in self.text.filter_headings():
			self.text.remove(heading)

	def deleteComments(self):
		for comment in self.text.filter_comments():
			self.text.remove(comment)

	def deleteTemplates(self):
		for template in self.text.filter_templates():
			try:
				self.text.remove(str(template))
			except ValueError:
				continue

	def replaceRefs(self):
		#set_trace()
		for ref in self.text.filter_tags(matches="^<ref"):
			self.text.replace(ref, "__REF__")
		for ref in self.text.filter_templates(matches="^{{sfn"):
			self.text.replace(ref, "__REF__")

	def deleteRefs(self):
		ref = re.compile(r"__REF__")
		self.text = mwp.parse(ref.sub(r'', str(self.text)))

	def deleteNoncontentHTML(self):
		for tag in self.text.filter_tags(matches="<blockquote|<math|<code"):
			self.text.remove(str(tag))

	def deleteMiscTags(self):
		for tag in set(self.text.filter_tags(matches="[^:|\*|#|;]")):
			try:
				self.text.replace(str(tag), tag.contents)
			except ValueError:
				continue

class NomPage(Text):
	def __init__(self, nompagetitle):
		super(NomPage, self).__init__(text=pwb.Page(site, nompagetitle).text)
		
		# Can't see why this should ever raise the exception, but for compliance
		checkbotsallowed(self.text)

		self.nompagetitle = nompagetitle
		self.nompage = pwb.Page(site, self.nompagetitle)
		self.nomtimestamp = self.nompage.oldest_revision.timestamp
		self.creators = set()
		self.nominators = set()
		self.article_titles = set()
		self.QPQs = set()
		self.nominations = []
		self.hooks = []
		self.statuses = {}
		self.errors = []

		# This should go somewhere else
		self.FreeImage = None
		self.HookLengths = []

	def checkAlreadyReviewed(self):
		num_editors = len(self.nompage.contributors())
		if num_editors > 3:
			return True
		elif num_editors == 1:
			return False
		else:
			return ("Symbol confirmed.svg" in self.text or  
			"Symbol voting keep.svg" in self.text or 
			"Symbol question.svg" in self.text or 
			"Symbol possible vote.svg" in self.text or 
			"Symbol delete vote.svg" in self.text or
			"Symbol redirect vote 4.svg" in self.text or
			"DYK checklist" in self.text or 
			"Symbol support vote.svg" in self.text)

	def parseHooks(self):
		hookrgx = re.compile(r"( \.\.\. that [^\n]+)\??\n")
		hooks = hookrgx.findall(str(self.text))
		if hooks == []:
			raise MalformedException("Failed to parse any hooks!")
		else:
			for hook in hooks:
				self.hooks.append(Hook(text=hook))

	def parseDYKmakes(self):
		for comment in self.text.filter_comments():
			# Needed to see templates in comments
			if "DYKmake" in comment:
				templates = mwp.parse(str(comment.contents)).filter_templates()
				for template in templates:
					if template.name == "DYKnom":
						self.nominators.add(str(template.get("2").value))
						self.article_titles.add(str(template.get("1").value))
					elif template.name == "DYKmake":
						self.creators.add(str(template.get("2").value))
						self.article_titles.add(str(template.get("1").value))
				if len(self.nominators) == 0:
					self.nominators = self.creators

				break
		else:
			raise MalformedException("Unable to find DYKmakes")

	def parseNominations(self):
		for article in self.article_titles:
			self.nominations.append(Nomination(article=article, nomtimestamp=self.nomtimestamp))

	def checkHookLengths(self):
		if len(self.nominations) > 1:
			self.correction_factor = sum([len(nom.article_title) for nom in self.nominations])
		else:
			self.correction_factor = 0

		for hook in self.hooks:
			self.HookLengths.append(hook.getReadableLength())

	def checkFreeImage(self):
		mpis = self.text.filter_templates(matches="main page image")
		if len(mpis) != 0:
			self.FreeImage = True
			for template in mpis:
				if template.name == "main page image":
					image = "File:" + str(template.get("image").value)
					self.image = image
					for cat in pwb.Page(site, image).categories():
						if str(cat) == "[[en:Category:All non-free media]]":
							self.FreeImage = False
							break

	def checkQPQ(self):
		self.nominator = list(self.nominators)[0]
		url = "https://tools.wmflabs.org/betacommand-dev/cgi-bin/dyk.py?user=" + str(self.nominator)
		qpq_page = urllib.urlopen(url).read()
		self.NomDYKs = len(qpq_page.split("<tr>")) - 1
		self.QPQ_needed = self.NomDYKs > 5

		if self.QPQ_needed:
			for wikilink in self.text.filter_wikilinks():
				if "Template:Did you know nominations/" in wikilink:
					self.QPQs.add(wikilink.title.split('/')[-1])
			if len(self.QPQs) >= len(self.nominations):
				self.QPQ_done = True
			else:
				regex_QPQs = re.findall("Template:Did you know nominations\/([^\]|\||\n]+)[\||\]|\n]", str(self.text))
				self.QPQs = self.QPQs.union(set(regex_QPQs))
				if len(self.QPQs) >= len(self.nominations):
					self.QPQ_done = True
				elif len(self.nominations) == 1:
					try:
						self.QPQs.add(re.search(r"eviewed'?'?:?\s+\[\[([^\]]+)\]\]", str(self.text)).group(1))
						self.QPQ_done = True
					except AttributeError:
						self.QPQ_done = False
				else:
					self.QPQ_done = False

		self.QPQs = ["[[Template:Did you know nominations/{}]]".format(qpq) for qpq in self.QPQs]

	def compile_comments(self, FORCE=False):
		self.comments = []
		for nomination in self.nominations:
			if len(self.nominations) > 1:
				self.comments.append("; Review of [[{article}]]".format(article=nomination.article_title))

			self.statuses[nomination.article_title] = nomination.checkNomination()
			self.comments += self.statuses[nomination.article_title].comments

		if len(self.nominations) > 1:
			self.comments.append("; General comments")

		if self.FreeImage is True:
			self.comments.append("*{{{{subst:y&}}}} The media [[:{image}]] is free-use".format(image=self.image))
		elif self.FreeImage is False: 
			self.comments.append("*{{{{subst:n&}}}} The media [[:{image}]] is nonfree".format(image=self.image))

		for i, hooklength in enumerate(self.HookLengths):
			if hooklength - self.correction_factor <= 200:
				self.comments.append("*{{{{subst:y&}}}} The hook ALT{i} is an appropriate length {multinom}at {chars} characters".format(
					chars=hooklength, i=i, multinom="for " + str(len(self.nominations)) + " nominations "))
			else:
				self.comments.append("*{{{{subst:n&}}}} The hook ALT{i} is too long at {chars} characters".format(chars=hooklength, i=i))

		if self.QPQ_needed is False:
			self.comments.append("*{{{{subst:y&}}}} This is [[User:{nominator}|]]'s' {num}th nomination. No QPQ required. Note a QPQ will be required after {more} more DYKs.".format(
				num=self.NomDYKs, more=5-self.NomDYKs, nominator=self.nominator[0]))
		elif self.QPQ_done:
			self.comments.append("*{{{{subst:y&}}}} This is [[User:{nominator}|]]'s {num}th nomination. {n_reviews} of {QPQs} {was} performed for this nomination.".format(
				num=self.NomDYKs, nominator=self.nominator, n_reviews='A QPQ review' if len(self.nominations) == 1 else str(len(self.nominations))+" QPQ reviews", was='was' if len(self.nominations) == 1 else 'were',
				QPQs=','.join(self.QPQs)))
		else:
			self.comments.append("*{{{{subst:n&}}}} This is [[User:{nominator}|]]'s {num}th nomination. {n_reviews} required for this nomination.".format(
				num=self.NomDYKs, nominator=self.nominator, n_reviews='A QPQ review is' if len(self.nominations) == 1 else str(len(self.nominations))+" QPQ reviews are",
				))

		self.comments.append("Automatically reviewed by [[User:DYKReviewBot|DYKReviewBot]]. This does '''not''' constitute a full review. ~~~~")

	def assess_issues(self):
		issues = [status.no_issues for status in self.statuses.values()]
		no_issues = sum(issues) == len(self.statuses.values())
		self.no_issues = no_issues and self.FreeImage is not False and filter(lambda x: x<200, self.HookLengths)

	def leaveNominationComments(self):
		edit_text = self.nompage.text.split('\n')
		print edit_text
		print self.comments
		edit_text.insert(-1, '\n'.join(self.comments))
		edit_text = '\n'.join(edit_text)
		self.nompage.text = edit_text
		summary = "No issues found" if self.no_issues else "There are issues which need to be addressed"
		# Debug only
		test_page = pwb.Page(site, u"User:Intelligentsium/"+self.nompagetitle)
		test_page.text = self.nompage.text
		test_page.save(summary="Automatically reviewing nomination: "+summary+" (BOT)")
	#	self.nompage.save(summary="Automatically reviewing nomination: "+summary+" (BOT)")

	def notifyNominators(self):
		for nominator in self.nominators:
			try:
				nominator_tp = pwb.Page(site, "User talk:"+str(nominator))
				checkbotsallowed(nominator_tp.text)
				nominator_tp.text += "\n{{subst:DYKproblem|{nompage}|header=yes|sig=yes}}".format(nompage=self.nompagetitle)
			#	nominator_tp.save("Notifying nominator of issues that need to be addressed re. [[{nompage}]] (BOT)".format(self.nompagetitle))
			except:
				self.nompage.text += "*[[Image:Pictogram voting info.svg|20px]] '''Bot note:''' Failed to notify nominator [[User:{nominator}]]. ~~~~".format(nominator=nominator)


	def review(self, FORCE=False):
		if self.checkAlreadyReviewed() is False or FORCE is True:
			self.parseHooks()
			self.parseDYKmakes()
			self.parseNominations()

			self.checkHookLengths()
			self.checkFreeImage()
			self.checkQPQ()

			self.compile_comments()
			self.assess_issues()
			self.leaveNominationComments()
			if not self.no_issues:
				self.notifyNominators()
		else:
			print "A user has already reviewed this nomination"
			self.statuses[self.nompagetitle] = DYKStatus(reviewed=True, nompagetitle=self.nompagetitle)

class Hook(Text):
	def __init__(self, text):
		super(Hook,self).__init__(text=text)

class Article(Text):
	def __init__(self, title=None, text=None):
		super(Article, self).__init__(text=text)
		self.title = title
		self.paragraphs = None
		self.readable_text = None
		self.lede_length = 1
		if title is not None:
			self.page = pwb.Page(site, title)
			self.text = mwp.parse(self.page.text)
			self.wikitext = mwp.parse(self.page.text) 

	def preprocess(self):
		# Parse wikisyntax
		self.deleteComments()
		self.deleteTables()
		self.replaceRefs() # Keep track of where refs are for citation check
		self.deleteTemplates()
		self.deleteLinks()
		self.deleteNoncontentHTML()
		self.deleteExtLinks()
		# Get number of ps in lede
		# This is a hack
		for i, para in enumerate(self.text.split('\n')):
			if para and para[0] == '=':
				self.lede_length = i - 1
				break

		self.deleteHeadings()
		self.deleteMiscTags()

		text_as_list = self.text.split('\n')
		# Remove null strings
		text_as_list = filter(lambda x: x != u'' and not x.isspace(), text_as_list)
		# Remove lists
		text_as_list = filter(lambda x: x[0] != u'*' and x[0] != u'#' and x[0] != u':' and x[0] != u';', text_as_list)

		self.paragraphs = text_as_list

		self.text = '\n'.join(text_as_list)
		self.deleteRefs()
		self.readable_text = self.text

class DYKStatus(object):
	def __init__(self, article_title=None, nompagetitle=None, reviewed=False, error=None):
		self.article_title = article_title
		self.nompagetitle = nompagetitle
		self.isNew = False
		self.BLP = False
		self.GA = False
		self.FreeImage = None  
		self.error = error 
		self.LongEnough = False 
		self.NewEnough = False
		self.NoCopyvio = False
		self.timestamp = None
		self.length = 0
		self.reviewed = reviewed
		self.UncitedParagraphs = []
		self.MaintenanceTags = []
		self.CopyvioPct = 0
		self.earwiglink = """https://tools.wmflabs.org/copyvios/"""
		self.comments = []

	def toXML(self):
		new_review = {}
		new_review['title'] = self.article_title
		new_review['reviewed'] = self.reviewed
		new_review['isNew'] = self.isNew
		new_review['GA'] = self.GA
		new_review['BLP'] = self.BLP
		new_review['LongEnough'] = self.LongEnough
		new_review['NewEnough'] = self.NewEnough
		new_review['CopyvioPct'] = self.CopyvioPct
		new_review['UncitedParagraphs'] = len(self.UncitedParagraphs)
		new_review['MaintenanceTags'] = len(self.MaintenanceTags)
		new_review['error'] = self.error
		new_review = {key:str(value).decode('utf-8') for key, value in new_review.iteritems()}
		self.xml = ET.Element('nomination', attrib=new_review)
		return self.xml

	def getStatus(self):
		self.no_issues = self.LongEnough and self.NewEnough and self.NoCopyvio and (len(self.UncitedParagraphs) == 0) and (len(self.MaintenanceTags) == 0)

	def review(self):
		print(self.article_title)
		self.getStatus()
		if self.no_issues is True:
			self.comments.append("*[[File:Symbol support vote.svg|16px]] '''No issues found.'''")
		else:
			self.comments.append("*[[File:Symbol question.svg|16px]] '''Some issues found.'''")
		if self.isNew:
			self.comments.append("**{{{{subst:y&}}}} This article is new and was created on {date:%H:%M, %d %B %Y} (UTC)".format(date=self.timestamp))
		elif self.NewEnough and self.BLP:
			self.comments.append("**{{{{subst:y&}}}} This biographical article has been expanded from {old_length} chars to {new_length} chars since {date:%H:%M, %d %B %Y} (UTC), a {fold:.2f}-fold expansion".format(
				old_length=self.old_length, new_length=self.length, date=self.timestamp, fold=self.length/float(self.old_length)))
		elif self.NewEnough and self.GA:
			self.comments.append("**{{{{subst:y&}}}} This article was [[Talk:{article_title}{action1link}|Listed]] as a Good Article on {date:%H:%M, %d %B %Y}".format(date=self.timestamp, article_title=self.article_title, action1link=self.GAN))			
		elif self.NewEnough:
			self.comments.append("**{{{{subst:y&}}}} This article has been expanded from {old_length} chars to {new_length} chars since {date:%H:%M, %d %B %Y} (UTC), a {fold:.2f}-fold expansion".format(
				old_length=self.old_length, new_length=self.length, date=self.timestamp, fold=self.length/float(self.old_length)))
		else:
			self.comments.append("**{{{{subst:n&}}}} This article was not created or 5x expanded within the past 7 days. This article has been expanded from {old_length} characters to {new_length} chars of readable prose since {date:%H:%M, %d %B %Y} (UTC), a {fold:.2f}-fold expansion, {chars} short of a 5x expansion.".format( \
				old_length=self.old_length, new_length=self.length, date=self.timestamp, fold=float(self.length)/self.old_length, chars=(5*self.old_length-self.length)))

		if self.LongEnough:
			self.comments.append("**{{{{subst:y&}}}} This article meets the DYK criteria at {} characters".format(self.length))
		else:
			self.comments.append("**{{{{subst:n&}}}} This article is too short at {} characters (the DYK minimum is 1500 characters)".format(self.length))

		if len(self.UncitedParagraphs) == 0:
			self.comments.append("**{{{{subst:y&}}}} All paragraphs in this article have at least one citation".format())
		else:
			self.comments.append("**{{{{subst:n&}}}} Paragraphs {} in this article lack a citation.".format(','.join(self.UncitedParagraphs)))
		if self.BLP:
			self.comments.append("***Note that this is a [[WP:BLP|biographical article about a living person]]. All claims must be cited to a [[WP:RS|reliable source]].".format())

		if len(self.MaintenanceTags) == 0:
			self.comments.append("**{{{{subst:y&}}}} This article has no outstanding maintenance tags".format())
		else:
			self.comments.append("**{{{{subst:n&}}}} This article has the following issues: ".format())
			for tag, date in self.MaintenanceTags:
				self.comments.append("***{{{{tlx|{tag}}}}} from {date}".format(tag=tag, date=date))

		if self.NoCopyvio:
			self.comments.append("**{{{{subst:y&}}}} The probability of copyright violation is {}%. ([{earwig} confirm])".format(self.CopyvioPct, earwig=self.earwiglink))
		else:
			self.comments.append("**{{{{subst:n&}}}} There is possible close paraphrasing on this article with {}% confidence. ([{earwig} confirm])".format(self.CopyvioPct,earwig=self.earwiglink))
		self.comments.append("***Note to reviewers: There is '''low confidence''' in this automated metric, please manually verify that there is no copyright infringement or close paraphrasing. Note that this number may be inflated due to cited quotes and titles which do ''not'' constitute a copyright violation.")

class Crawler(Text):
	def __init__(self):
		self.page = pwb.Page(site, "Template talk:Did you know")
		super(Crawler,self).__init__(text=self.page.text)

	def preprocess(self):
		self.nominations = [str(entry.name) for entry in self.text.filter_templates() if "Did you know nominations" in entry.name]

	def getNomPage(self, rawnom):
		nom = "Template:Did you know nominations/" + re.search(r'(Template:)?Did you know nominations\/([^\}]+)', rawnom).group(2)
		return nom

	def readlog(self):
		self.reviewed = []
		self.logtree = ET.parse('log.xml')
		self.logroot = self.logtree.getroot()
		for run in self.logroot.findall("run"):
			for nomination in run.findall("nomination"):
				self.reviewed.append(nomination.attrib['nompage_title'])

	def writelog(self):
		new_run = ET.Element('run')
		new_run.set('timestamp', str(datetime.now()))
		for nomination in self.new_reviews:
			new_run.append(nomination)
		self.logroot.append(new_run)
		self.logtree.write('log.xml')
		self.new_reviews = []

	def run(self):
		self.readlog()
		self.preprocess()
		self.new_reviews = []
		# DEBUG!
		i = 0
		for nomination in self.nominations[-1:0:-1]:
			i += 1
			try:
				nompage = self.getNomPage(nomination)
				if nompage not in self.reviewed:
					try:
						nom = NomPage(nompage)
						nom.review()
					except MalformedException as e:
						print 'Nomination {} malformed'.format(e.page)
					self.new_reviews += [status.toXML() for status in nom.statuses.values()]
				if i % 10 == 6:
					self.writelog()
			except pwb.NoPage:
				print(u"{} not found - possibly malformed nomination".format(nompage))
			#except:
			#	print u"{} failed to parse - possibly malformed nomination".format(nompage)

if __name__ == "__main__":
	c = Crawler()
	c.run()